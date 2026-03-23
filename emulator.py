#!/usr/bin/env python3
"""
Soundmeter UART Protocol v2 — device emulator for testing.

Linux setup:
    pip install pyserial
    socat -d -d pty,raw,echo=0 pty,raw,echo=0
    # socat prints two port names, e.g. /dev/pts/3 and /dev/pts/4
    # Run emulator on one, soundmeter.py on the other:
    python emulator.py   --port /dev/pts/3
    python soundmeter.py --port /dev/pts/4

Windows setup:
    Install com0com (creates COM10 <-> COM11 pair)
    python emulator.py   --port COM10
    python soundmeter.py --port COM11
"""
import argparse
import struct
import math
import random
import time
import logging

import serial

logging.basicConfig(level=logging.INFO, format='%(asctime)s EMU %(levelname)s %(message)s')
log = logging.getLogger(__name__)

FRAME_DEVICE = 0x7A   # direction: device → PC
FRAME_CLIENT = 0x7B   # direction: PC → device

CMD_PING          = 0x0000
CMD_LOCK_KB       = 0x0001
CMD_READ_VERSIONS = 0x0002
CMD_SET_TEMPLATE  = 0x0102
CMD_READ_TEMPLATE = 0x0201
CMD_SET_MODE      = 0x0208


# ── CRC (same as soundmeter.py) ───────────────────────────────────────────────

def crc16(data: bytes) -> int:
    if len(data) % 2:
        data += b'\x00'
    s = 0
    for i in range(0, len(data), 2):
        s += struct.unpack_from('<H', data, i)[0]
    while s >> 16:
        s = (s & 0xFFFF) + (s >> 16)
    return (~s) & 0xFFFF


# ── Packet helpers ────────────────────────────────────────────────────────────

def build_response(addr: int, command: int, payload: bytes = b'', packet_id: int = 0) -> bytes:
    """Build response packet (direction: device → PC, command = request_cmd | 0x8000)."""
    if payload and len(payload) % 2:
        payload += b'\x00'
    data_len = (len(payload) + 2) if payload else 0
    header_body = struct.pack('<BBHHHHH',
        FRAME_DEVICE, addr, command, data_len, packet_id, 0, 0)
    crc_h = crc16(header_body[:10])
    header = header_body[:10] + struct.pack('<H', crc_h)
    if payload:
        crc_d = crc16(payload)
        return header + payload + struct.pack('<H', crc_d)
    return header


def parse_request(raw: bytes) -> dict | None:
    """Parse incoming client request. Returns dict or None."""
    if len(raw) < 12:
        return None
    frame, addr, command, data_len, packet_id, reserved = struct.unpack_from('<BBHHHH', raw, 0)
    crc_h = struct.unpack_from('<H', raw, 10)[0]
    payload = b''
    if data_len > 0:
        payload = raw[12:12 + data_len - 2]  # strip CRC_data
    return {
        'addr': addr,
        'command': command,
        'data_len': data_len,
        'packet_id': packet_id,
        'payload': payload,
    }


# ── Fake noise generator ──────────────────────────────────────────────────────

def fract88(db_value: float) -> int:
    """Encode float dB value to signed fract 8.8 word."""
    raw = int(db_value * 256) & 0xFFFF
    return raw


def fake_db(base: float, noise: float = 2.0) -> int:
    """Random dB value around base ± noise."""
    return fract88(base + random.uniform(-noise, noise))


def generate_measurement_words(num_params: int) -> bytes:
    """Generate fake measurement data: num_params fract 8.8 words."""
    t = time.time()
    # Simulate a slowly varying noise floor ~65 dB with some variation
    base_a   = 65.0 + 5.0 * math.sin(t / 10)
    base_lin = base_a + 3.0
    base_c   = base_a + 1.5

    words = []
    for i in range(num_params):
        # Mix: octave bands lower level, weighted sums higher
        if i < 180:
            # 1/3 octave: lower individual levels
            w = fake_db(base_a - 10 + random.uniform(-5, 5))
        elif i < 240:
            # 1/1 octave
            w = fake_db(base_a - 5 + random.uniform(-3, 3))
        else:
            # Weighted A/C/Lin/G
            w = fake_db(base_a + random.uniform(-1, 1))
        words.append(w)

    return struct.pack(f'<{len(words)}H', *words)


# ── Device state ──────────────────────────────────────────────────────────────

class DeviceState:
    def __init__(self):
        self.kb_locked = False
        self.mode = 50  # SLM
        self.templates: dict[int, int] = {}  # template_id → num_params_requested

    def count_params(self, bitmask: bytes) -> int:
        """Count set bits in bitmask."""
        return sum(bin(b).count('1') for b in bitmask)


# ── Main emulator loop ────────────────────────────────────────────────────────

def run(port: str, baud: int, addr: int):
    ser = serial.Serial(port, baud, timeout=0.1)
    state = DeviceState()
    log.info(f"Emulator listening on {port} @ {baud} baud, device addr={addr}")
    log.info("Waiting for commands...")

    buf = b''
    while True:
        chunk = ser.read(256)
        if not chunk:
            continue
        buf += chunk

        # Need at least header (12 bytes)
        while len(buf) >= 12:
            frame, _, _, data_len = struct.unpack_from('<BBHH', buf, 0)

            if frame != FRAME_CLIENT:
                # Resync: skip 1 byte
                buf = buf[1:]
                continue

            total_len = 12 + data_len
            if len(buf) < total_len:
                break  # wait for more bytes

            raw = buf[:total_len]
            buf = buf[total_len:]

            req = parse_request(raw)
            if req is None:
                continue

            cmd = req['command']
            pid = req['packet_id']
            pld = req['payload']

            log.info(f"CMD 0x{cmd:04X}  payload={pld.hex() if pld else '(none)'}")

            # ── Handle each command ──────────────────────────────────────────

            if cmd == CMD_PING:
                resp = build_response(addr, cmd | 0x8000, packet_id=pid)
                ser.write(resp)
                log.info("  → PONG")

            elif cmd == CMD_LOCK_KB:
                state.kb_locked = not state.kb_locked
                resp = build_response(addr, cmd | 0x8000, packet_id=pid)
                ser.write(resp)
                log.info(f"  → Keyboard {'LOCKED' if state.kb_locked else 'UNLOCKED'}")

            elif cmd == CMD_READ_VERSIONS:
                # version 1.0 for everything
                data = struct.pack('<HHH', 0x0100, 0x0100, 0x0100)
                crc  = struct.pack('<H', crc16(data))
                resp = build_response(addr, cmd | 0x8000, data + crc, packet_id=pid)
                ser.write(resp)
                log.info("  → Version 1.0 / 1.0 / 1.0")

            elif cmd == CMD_SET_TEMPLATE:
                if len(pld) >= 2:
                    tmpl_id = struct.unpack_from('<H', pld, 0)[0]
                    bitmask = pld[2:]
                    n = state.count_params(bitmask)
                    state.templates[tmpl_id] = n
                    resp = build_response(addr, cmd | 0x8000, packet_id=pid)
                    ser.write(resp)
                    log.info(f"  → Template {tmpl_id} configured: {n} params")
                else:
                    _send_error(ser, addr, 0xFF11, pid)

            elif cmd == CMD_READ_TEMPLATE:
                if len(pld) >= 2:
                    tmpl_id = struct.unpack_from('<H', pld, 0)[0]
                    n = state.templates.get(tmpl_id, 50)
                    data = generate_measurement_words(n)
                    resp = build_response(addr, cmd | 0x8000, data, packet_id=pid)
                    ser.write(resp)
                    log.info(f"  → {n} measurement words sent")
                else:
                    _send_error(ser, addr, 0xFF11, pid)

            elif cmd == CMD_SET_MODE:
                if len(pld) >= 2:
                    mode = struct.unpack_from('<H', pld, 0)[0]
                    state.mode = mode
                    resp = build_response(addr, cmd | 0x8000, packet_id=pid)
                    ser.write(resp)
                    log.info(f"  → Mode set to {mode}")
                else:
                    _send_error(ser, addr, 0xFF1C, pid)

            else:
                _send_error(ser, addr, 0xFF07, pid)
                log.warning(f"  → Unknown command, sent 0xFF07")


def _send_error(ser, addr: int, error_code: int, packet_id: int):
    resp = build_response(addr, error_code, packet_id=packet_id)
    ser.write(resp)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Soundmeter device emulator')
    parser.add_argument('--port', default='/dev/pts/3', help='Serial port for emulator side')
    parser.add_argument('--baud', type=int, default=115200)
    parser.add_argument('--addr', type=int, default=1)
    args = parser.parse_args()
    run(args.port, args.baud, args.addr)
