#!/usr/bin/env python3
"""
Soundmeter data logger — UART Protocol v2
Reads acoustic measurements, saves to SQLite DB.

Usage:
    python soundmeter.py --port COM3          # Windows
    python soundmeter.py --port /dev/ttyUSB0  # Linux
    python soundmeter.py --port COM3 --baud 115200 --addr 1 --db noise.db
"""
import argparse
import struct
import time
import json
import sqlite3
import logging
from datetime import datetime
from typing import Optional

import serial

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
log = logging.getLogger(__name__)

# ── Protocol constants ────────────────────────────────────────────────────────

FRAME_CLIENT = 0x7B   # packet from PC to device
FRAME_DEVICE = 0x7A   # packet from device to PC

CMD_PING          = 0x0000
CMD_LOCK_KB       = 0x0001
CMD_SET_TEMPLATE  = 0x0102
CMD_READ_TEMPLATE = 0x0201
CMD_SET_MODE      = 0x0208

MODE_SLM = 50   # standard sound level meter screen

TEMPLATE_ID = 0  # use template slot 0

# ── CRC (RFC 1071, little-endian words) ───────────────────────────────────────

def crc16(data: bytes) -> int:
    if len(data) % 2:
        data += b'\x00'
    s = 0
    for i in range(0, len(data), 2):
        s += struct.unpack_from('<H', data, i)[0]
    while s >> 16:
        s = (s & 0xFFFF) + (s >> 16)
    return (~s) & 0xFFFF

# ── Packet builder ────────────────────────────────────────────────────────────

def build_packet(addr: int, command: int, payload: bytes = b'', packet_id: int = 0) -> bytes:
    """
    Build request packet.
    Header (12 bytes): FrameType | Addr | Command | DataLen | PacketID | Reserved | CRC_hdr
    Data section (optional): payload + CRC_data
    DataLen = len(payload) + 2  (includes CRC_data)
    """
    if payload and len(payload) % 2:
        payload += b'\x00'  # pad to even length

    data_len = (len(payload) + 2) if payload else 0

    header_body = struct.pack('<BBHHHHH',
        FRAME_CLIENT,  # frame type
        addr,          # device address
        command,       # command code
        data_len,      # length of data section (incl. CRC_data)
        packet_id,     # packet ID
        0,             # reserved
        0,             # placeholder for CRC_hdr (will be overwritten)
    )
    # CRC covers first 10 bytes (before CRC_hdr field)
    crc_h = crc16(header_body[:10])
    header = header_body[:10] + struct.pack('<H', crc_h)  # 12 bytes

    if payload:
        crc_d = crc16(payload)
        return header + payload + struct.pack('<H', crc_d)
    return header


def parse_header(raw: bytes) -> Optional[dict]:
    """Parse 12-byte response header. Returns None on CRC error."""
    if len(raw) < 12:
        return None
    frame, addr, command, data_len, packet_id, reserved = struct.unpack_from('<BBHHHH', raw, 0)
    crc_h = struct.unpack_from('<H', raw, 10)[0]

    expected = crc16(raw[:10])
    if expected != crc_h:
        log.warning(f"Header CRC mismatch: got 0x{crc_h:04X}, expected 0x{expected:04X}")
        # Don't abort — some devices compute CRC differently; log and continue

    return {
        'frame':    frame,
        'addr':     addr,
        'command':  command,
        'data_len': data_len,
        'packet_id': packet_id,
    }

# ── fract 8.8 decoder ─────────────────────────────────────────────────────────

def decode_fract88(word: int) -> float:
    """Signed fract 8.8 fixed-point → float (dB). Range: -128.00 … +127.996"""
    signed = struct.unpack('<h', struct.pack('<H', word & 0xFFFF))[0]
    return round(signed / 256.0, 2)

# ── Parameter map (bit index → name) ──────────────────────────────────────────

def _build_param_map() -> dict:
    m = {}

    # 1/3 octave bands (noise meter), bit groups of 6
    oct3_freqs = [
        20000, 10000, 5000, 2500, 1250, 630, 315, 160, 80, 40,
        16000, 8000, 4000, 2000, 1000, 500, 250, 125, 63, 31,
        12500, 6300, 3150, 1600, 800, 400, 200, 100, 50, 25,
    ]
    for i, freq in enumerate(oct3_freqs):
        b = i * 6
        m[b]   = f"oct3_{freq}hz_rms"
        m[b+1] = f"oct3_{freq}hz_rms1s"
        # b+2 = reserve
        m[b+3] = f"oct3_{freq}hz_exp"
        m[b+4] = f"oct3_{freq}hz_min"
        m[b+5] = f"oct3_{freq}hz_max"

    # 1/1 octave bands, starting at bit 180
    oct1_freqs = [16000, 8000, 4000, 2000, 1000, 500, 250, 125, 63, 31]
    for i, freq in enumerate(oct1_freqs):
        b = 180 + i * 6
        m[b]   = f"oct1_{freq}hz_rms"
        m[b+1] = f"oct1_{freq}hz_rms1s"
        m[b+3] = f"oct1_{freq}hz_exp"
        m[b+4] = f"oct1_{freq}hz_min"
        m[b+5] = f"oct1_{freq}hz_max"

    # Weighted measurements: Lin=240, C=252, A=264, G=276
    for prefix, b in [("lin", 240), ("c", 252), ("a", 264), ("g", 276)]:
        m[b]    = f"{prefix}_rms"
        m[b+1]  = f"{prefix}_rms1s"
        # b+2 = reserve
        m[b+3]  = f"{prefix}_slow"
        m[b+4]  = f"{prefix}_slow_min"
        m[b+5]  = f"{prefix}_slow_max"
        m[b+6]  = f"{prefix}_fast"
        m[b+7]  = f"{prefix}_fast_min"
        m[b+8]  = f"{prefix}_fast_max"
        m[b+9]  = f"{prefix}_imp"
        m[b+10] = f"{prefix}_imp_min"
        m[b+11] = f"{prefix}_imp_max"

    # System parameters (raw integer, not fract 8.8)
    m[438] = "battery_level"
    m[511] = "cur_meas_mode"

    return m


PARAM_MAP = _build_param_map()

# Sorted list of bit indices we want to read
REQUESTED_BITS = sorted(PARAM_MAP.keys())

# Which params are raw integers (not dB fract 8.8)
RAW_INT_PARAMS = {"battery_level", "cur_meas_mode"}


def build_bitmask() -> bytes:
    """Build 320-byte (160-word) bitmask: 1 bit per requested parameter."""
    mask = bytearray(320)
    for bit in REQUESTED_BITS:
        word_idx = bit // 16
        if word_idx >= 160:
            continue
        offset = word_idx * 2
        current = struct.unpack_from('<H', mask, offset)[0]
        struct.pack_into('<H', mask, offset, current | (1 << (bit % 16)))
    return bytes(mask)


def parse_response_data(payload: bytes) -> dict:
    """
    Parse data payload from CMD_READ_TEMPLATE response.
    Values arrive in order of increasing bit index.
    Last 2 bytes of payload = CRC_data (stripped).
    """
    data = payload[:-2]  # strip CRC_data
    values = {}
    num_words = len(data) // 2
    for i, bit in enumerate(REQUESTED_BITS):
        if i >= num_words:
            break
        word = struct.unpack_from('<H', data, i * 2)[0]
        name = PARAM_MAP[bit]
        if name in RAW_INT_PARAMS:
            values[name] = word
        else:
            values[name] = decode_fract88(word)
    return values

# ── Device communication ──────────────────────────────────────────────────────

class SoundMeter:
    def __init__(self, port: str, baud: int = 115200, addr: int = 1, timeout: float = 3.0):
        self.addr = addr
        self._packet_id = 0
        self.ser = serial.Serial(port, baud, timeout=timeout)
        log.info(f"Connected to {port} @ {baud} baud, device addr={addr}")

    def _next_id(self) -> int:
        self._packet_id = (self._packet_id + 1) & 0xFFFF
        return self._packet_id

    def _send_recv(self, command: int, payload: bytes = b'') -> Optional[bytes]:
        """Send request, return data payload (field 8 + CRC_data) or None on error."""
        pkt = build_packet(self.addr, command, payload, self._next_id())
        self.ser.reset_input_buffer()
        self.ser.write(pkt)

        header_raw = self.ser.read(12)
        if len(header_raw) < 12:
            log.warning(f"Timeout reading header (cmd=0x{command:04X})")
            return None

        hdr = parse_header(header_raw)
        if hdr is None:
            return None

        cmd_resp = hdr['command']
        if cmd_resp >= 0xFF00:
            log.error(f"Device error 0x{cmd_resp:04X} for cmd 0x{command:04X}")
            return None

        expected_resp = command | 0x8000
        if cmd_resp != expected_resp:
            log.warning(f"Unexpected response: 0x{cmd_resp:04X} (expected 0x{expected_resp:04X})")
            return None

        if hdr['data_len'] == 0:
            return b''

        data = self.ser.read(hdr['data_len'])
        if len(data) < hdr['data_len']:
            log.warning("Timeout reading data section")
            return None

        return data

    # ── Commands ──────────────────────────────────────────────────────────────

    def ping(self) -> bool:
        ok = self._send_recv(CMD_PING) is not None
        log.info(f"Ping: {'OK' if ok else 'FAILED'}")
        return ok

    def lock_keyboard(self) -> bool:
        """Toggle keyboard lock. Must be called once before set_mode/read_data."""
        ok = self._send_recv(CMD_LOCK_KB) is not None
        log.info(f"Lock keyboard: {'OK' if ok else 'FAILED'}")
        return ok

    def set_mode(self, mode: int = MODE_SLM) -> bool:
        """Switch device to measurement mode (requires keyboard locked first)."""
        payload = struct.pack('<H', mode)
        ok = self._send_recv(CMD_SET_MODE, payload) is not None
        log.info(f"Set mode {mode}: {'OK' if ok else 'FAILED'}")
        return ok

    def configure_template(self) -> bool:
        """One-time setup: tell device which parameters to include in each response."""
        bitmask = build_bitmask()
        payload = struct.pack('<H', TEMPLATE_ID) + bitmask  # 2 + 320 = 322 bytes
        ok = self._send_recv(CMD_SET_TEMPLATE, payload) is not None
        log.info(f"Configure template (slot {TEMPLATE_ID}, {len(REQUESTED_BITS)} params): {'OK' if ok else 'FAILED'}")
        return ok

    def read_data(self) -> Optional[dict]:
        """Read current measurements using the configured template."""
        payload = struct.pack('<H', TEMPLATE_ID)
        data = self._send_recv(CMD_READ_TEMPLATE, payload)
        if data is None or len(data) < 4:
            return None
        return parse_response_data(data)

    def close(self):
        self.ser.close()
        log.info("Port closed")

# ── SQLite storage ────────────────────────────────────────────────────────────

class Storage:
    def __init__(self, db_path: str):
        self.conn = sqlite3.connect(db_path)
        self.conn.execute('''
            CREATE TABLE IF NOT EXISTS measurements (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT    NOT NULL,
                data      TEXT    NOT NULL
            )
        ''')
        self.conn.commit()
        log.info(f"Database: {db_path}")

    def save(self, data: dict):
        ts = datetime.now().isoformat(timespec='seconds')
        self.conn.execute(
            'INSERT INTO measurements (timestamp, data) VALUES (?, ?)',
            (ts, json.dumps(data))
        )
        self.conn.commit()

    def close(self):
        self.conn.close()

# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description='Soundmeter UART data logger')
    parser.add_argument('--port',     default='COM3',           help='Serial port (COM3 or /dev/ttyUSB0)')
    parser.add_argument('--baud',     type=int, default=115200,  help='Baud rate')
    parser.add_argument('--addr',     type=int, default=1,       help='Device address (0-255)')
    parser.add_argument('--db',       default='soundmeter.db',   help='SQLite database file')
    parser.add_argument('--interval', type=float, default=1.0,   help='Polling interval, seconds')
    args = parser.parse_args()

    db    = Storage(args.db)
    meter = SoundMeter(args.port, args.baud, args.addr)

    try:
        if not meter.ping():
            log.error("Device not responding. Check port and cable.")
            return

        meter.lock_keyboard()       # required before switching modes
        meter.set_mode(MODE_SLM)    # switch to SLM measurement screen
        meter.configure_template()  # one-time: define which params to read

        log.info(f"Collecting data every {args.interval}s — press Ctrl+C to stop")
        while True:
            t0 = time.time()

            data = meter.read_data()
            if data:
                db.save(data)
                log.info(
                    f"A={data.get('a_rms','?'):6} dB  "
                    f"C={data.get('c_rms','?'):6} dB  "
                    f"Lin={data.get('lin_rms','?'):6} dB  "
                    f"[{len(data)} params saved]"
                )
            else:
                log.warning("No data received from device")

            elapsed = time.time() - t0
            time.sleep(max(0.0, args.interval - elapsed))

    except KeyboardInterrupt:
        log.info("Stopped.")
    finally:
        meter.close()
        db.close()


if __name__ == '__main__':
    main()
