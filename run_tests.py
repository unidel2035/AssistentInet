#!/usr/bin/env python3
"""
Self-contained protocol test.
Uses in-memory queues as virtual serial bus — no hardware, socat or PTY needed.
"""
import queue
import struct
import threading
import time
import math
import random
import sys

# ── Colors ────────────────────────────────────────────────────────────────────
G = "\033[92m"; R = "\033[91m"; Y = "\033[93m"
B = "\033[94m"; RST = "\033[0m"; BOLD = "\033[1m"

passed = failed = 0

def ok(m):   print(f"  {G}✓ PASS{RST}  {m}")
def err(m):  print(f"  {R}✗ FAIL{RST}  {m}")
def inf(m):  print(f"  {Y}ℹ{RST}      {m}")
def hdr(m):  print(f"\n{BOLD}{B}── {m}{RST}")

def chk(cond, label, detail=""):
    global passed, failed
    msg = label + (f"  [{detail}]" if detail else "")
    if cond: ok(msg);  passed += 1
    else:    err(msg); failed += 1
    return cond

# ── CRC RFC 1071 ──────────────────────────────────────────────────────────────

def crc16(data: bytes) -> int:
    if len(data) % 2: data += b'\x00'
    s = 0
    for i in range(0, len(data), 2):
        s += struct.unpack_from('<H', data, i)[0]
    while s >> 16: s = (s & 0xFFFF) + (s >> 16)
    return (~s) & 0xFFFF

# ── Packet helpers ────────────────────────────────────────────────────────────

_pid = 0
def build_packet(addr, cmd, payload=b''):
    global _pid
    _pid = (_pid + 1) & 0xFFFF
    if payload and len(payload) % 2: payload += b'\x00'
    dl = (len(payload) + 2) if payload else 0
    hb = struct.pack('<BBHHHHH', 0x7B, addr, cmd, dl, _pid, 0, 0)
    h  = hb[:10] + struct.pack('<H', crc16(hb[:10]))
    return h + payload + struct.pack('<H', crc16(payload)) if payload else h

def build_response(addr, cmd, payload=b'', pid=0):
    if payload and len(payload) % 2: payload += b'\x00'
    dl = (len(payload) + 2) if payload else 0
    hb = struct.pack('<BBHHHHH', 0x7A, addr, cmd, dl, pid, 0, 0)
    h  = hb[:10] + struct.pack('<H', crc16(hb[:10]))
    return h + payload + struct.pack('<H', crc16(payload)) if payload else h

def parse_packet(raw: bytes):
    if len(raw) < 12: return None, None, False, 0
    frame, addr, cmd, dl, pid, _ = struct.unpack_from('<BBHHHH', raw, 0)
    crc_h = struct.unpack_from('<H', raw, 10)[0]
    crc_ok = (crc16(raw[:10]) == crc_h)
    if dl == 0: return cmd, b'', crc_ok, pid
    payload = raw[12:12 + dl - 2]
    crc_d   = struct.unpack_from('<H', raw, 12 + dl - 2)[0]
    return cmd, payload, crc_ok and (crc16(payload) == crc_d), pid

# ── Virtual serial bus ────────────────────────────────────────────────────────

class VirtualBus:
    """Two queues simulating a serial cable between client and device."""
    def __init__(self):
        self.c2d = queue.Queue()  # client → device
        self.d2c = queue.Queue()  # device → client

    def client_send(self, data: bytes): self.c2d.put(data)
    def client_recv(self, timeout=3.0): return self.d2c.get(timeout=timeout)
    def device_recv(self, timeout=3.0): return self.c2d.get(timeout=timeout)
    def device_send(self, data: bytes): self.d2c.put(data)

# ── Emulator ──────────────────────────────────────────────────────────────────

def fract88(v): return int(v * 256) & 0xFFFF

def run_emulator(bus: VirtualBus, addr: int, stop: threading.Event):
    kb_locked = False
    templates = {}
    mode = 50
    while not stop.is_set():
        try:
            raw = bus.device_recv(timeout=0.1)
        except queue.Empty:
            continue
        cmd, payload, _, pid = parse_packet(raw)
        if cmd is None: continue

        if   cmd == 0x0000:  # Ping
            bus.device_send(build_response(addr, 0x8000, pid=pid))
        elif cmd == 0x0001:  # Lock KB
            kb_locked = not kb_locked
            bus.device_send(build_response(addr, 0x8001, pid=pid))
        elif cmd == 0x0002:  # Versions
            bus.device_send(build_response(addr, 0x8002,
                struct.pack('<HHH', 0x0100, 0x0100, 0x0100), pid=pid))
        elif cmd == 0x0003:  # Set baud
            bus.device_send(build_response(addr, 0x8003, pid=pid))
        elif cmd == 0x0004:  # Set address
            bus.device_send(build_response(addr, 0x8004, pid=pid))
        elif cmd == 0x0005:  # Reset
            bus.device_send(build_response(addr, 0x8005, pid=pid))
        elif cmd == 0x0006:  # Power off
            bus.device_send(build_response(addr, 0x8006, pid=pid))
        elif cmd == 0x0009:  # Device number
            bus.device_send(build_response(addr, 0x8009,
                struct.pack('<I', 0xDEAD1234), pid=pid))
        elif cmd == 0x0100:  # Set time
            bus.device_send(build_response(addr, 0x8100, pid=pid))
        elif cmd == 0x0101:  # Set date
            bus.device_send(build_response(addr, 0x8101, pid=pid))
        elif cmd == 0x0102:  # Set template
            if len(payload) >= 2:
                tmpl_id = struct.unpack_from('<H', payload, 0)[0]
                n = sum(bin(b).count('1') for b in payload[2:])
                templates[tmpl_id] = n
            bus.device_send(build_response(addr, 0x8102, pid=pid))
        elif cmd == 0x0201:  # Read data
            if len(payload) >= 2:
                tid = struct.unpack_from('<H', payload, 0)[0]
                n = templates.get(tid, 2)
                base = 65.0 + 5.0 * math.sin(time.time() / 10)
                words = [fract88(base + random.uniform(-2, 2)) for _ in range(n)]
                bus.device_send(build_response(addr, 0x8201,
                    struct.pack(f'<{n}H', *words), pid=pid))
            else:
                bus.device_send(build_response(addr, 0xFF11, pid=pid))
        elif cmd == 0x0208:  # Set mode
            if len(payload) >= 2:
                mode = struct.unpack_from('<H', payload, 0)[0]
                bus.device_send(build_response(addr, 0x8208, pid=pid))
            else:
                bus.device_send(build_response(addr, 0xFF1C, pid=pid))
        else:
            bus.device_send(build_response(addr, 0xFF07, pid=pid))

# ── Test client ───────────────────────────────────────────────────────────────

def send(bus, addr, cmd, payload=b''):
    pkt = build_packet(addr, cmd, payload)
    bus.client_send(pkt)
    try:
        raw = bus.client_recv(timeout=3.0)
        return parse_packet(raw)
    except queue.Empty:
        return None, None, False, 0

def decode88(w):
    return round(struct.unpack('<h', struct.pack('<H', w & 0xFFFF))[0] / 256.0, 2)

def run_tests(bus, addr=1):

    # ── 0x0000 Ping ───────────────────────────────────────────────────────────
    hdr("0x0000 — Ping")
    cmd, data, crc_ok, _ = send(bus, addr, 0x0000)
    chk(cmd == 0x8000, "Ответный код 0x8000", f"получен 0x{cmd:04X}" if cmd else "нет ответа")
    chk(data == b'',   "Нет данных в ответе")
    chk(crc_ok,        "CRC заголовка верна")

    # ── 0x0001 Блокировка клавиатуры ─────────────────────────────────────────
    hdr("0x0001 — Блокировка клавиатуры")
    cmd, data, crc_ok, _ = send(bus, addr, 0x0001)
    chk(cmd == 0x8001, "Ответный код 0x8001")
    chk(data == b'',   "Нет данных в ответе")
    chk(crc_ok,        "CRC верна")
    inf("Клавиатура заблокирована (повторный вызов — разблокировка)")

    # ── 0x0002 Версии ─────────────────────────────────────────────────────────
    hdr("0x0002 — Чтение версий прибора")
    cmd, data, crc_ok, _ = send(bus, addr, 0x0002)
    chk(cmd == 0x8002,    "Ответный код 0x8002")
    chk(len(data) == 6,   "Длина данных 6 байт", f"получено {len(data)}")
    chk(crc_ok,           "CRC верна")
    if data and len(data) >= 6:
        vs, vv, vb = struct.unpack_from('<HHH', data)
        inf(f"Шумомер  {vs>>8}.{vs&0xFF}  |  Виброметр {vv>>8}.{vv&0xFF}  |  BIOS {vb>>8}.{vb&0xFF}")

    # ── 0x0003 Скорость UART ──────────────────────────────────────────────────
    hdr("0x0003 — Установка скорости UART (код 0 = 115200)")
    bauds = [(0,"115200"),(1,"57600"),(2,"38400"),(3,"28800"),(4,"19200"),(5,"14400"),(6,"9600")]
    for code, label in bauds:
        cmd, data, crc_ok, _ = send(bus, addr, 0x0003, struct.pack('<H', code))
        chk(cmd == 0x8003, f"Код {code} ({label} бод) → 0x8003")

    # ── 0x0004 Адрес ──────────────────────────────────────────────────────────
    hdr("0x0004 — Установка адреса прибора")
    cmd, data, crc_ok, _ = send(bus, addr, 0x0004, struct.pack('<H', addr))
    chk(cmd == 0x8004, "Ответный код 0x8004")
    chk(data == b'',   "Нет данных в ответе")
    chk(crc_ok,        "CRC верна")

    # ── 0x0009 Номер прибора ──────────────────────────────────────────────────
    hdr("0x0009 — Чтение номера прибора")
    cmd, data, crc_ok, _ = send(bus, addr, 0x0009)
    chk(cmd == 0x8009,   "Ответный код 0x8009")
    chk(len(data) == 4,  "Длина данных 4 байта", f"получено {len(data)}")
    chk(crc_ok,          "CRC верна")
    if data and len(data) >= 4:
        inf(f"Номер прибора: 0x{struct.unpack_from('<I', data)[0]:08X}")

    # ── 0x0100 Время ──────────────────────────────────────────────────────────
    hdr("0x0100 — Установка времени 12:34:56")
    cmd, data, crc_ok, _ = send(bus, addr, 0x0100, struct.pack('<HH', (34<<8)|56, 12))
    chk(cmd == 0x8100, "Ответный код 0x8100")
    chk(data == b'',   "Нет данных в ответе")
    chk(crc_ok,        "CRC верна")

    # ── 0x0101 Дата ───────────────────────────────────────────────────────────
    hdr("0x0101 — Установка даты 23.03.2026")
    cmd, data, crc_ok, _ = send(bus, addr, 0x0101, struct.pack('<HH', (3<<8)|23, 26))
    chk(cmd == 0x8101, "Ответный код 0x8101")
    chk(data == b'',   "Нет данных в ответе")
    chk(crc_ok,        "CRC верна")

    # ── 0x0102 Шаблон 0: 2 параметра ─────────────────────────────────────────
    hdr("0x0102 — Шаблон 0: dB(Lin) RMS [бит 240] + dB(A) RMS [бит 264]")
    mask = bytearray(320)
    for bit in [240, 264]:
        wi = bit // 16
        struct.pack_into('<H', mask, wi*2,
            struct.unpack_from('<H', mask, wi*2)[0] | (1 << (bit % 16)))
    cmd, data, crc_ok, _ = send(bus, addr, 0x0102, struct.pack('<H', 0) + bytes(mask))
    chk(cmd == 0x8102, "Ответный код 0x8102")
    chk(data == b'',   "Нет данных в ответе")
    chk(crc_ok,        "CRC верна")

    # ── 0x0201 Чтение по шаблону 0 ───────────────────────────────────────────
    hdr("0x0201 — Чтение данных по шаблону 0 (2 параметра)")
    cmd, data, crc_ok, _ = send(bus, addr, 0x0201, struct.pack('<H', 0))
    chk(cmd == 0x8201,   "Ответный код 0x8201")
    chk(len(data) == 4,  "4 байта (2 × word fract 8.8)", f"получено {len(data)}")
    chk(crc_ok,          "CRC верна")
    if data and len(data) >= 4:
        lin = decode88(struct.unpack_from('<H', data, 0)[0])
        a   = decode88(struct.unpack_from('<H', data, 2)[0])
        chk(-10 <= lin <= 200, f"dB(Lin) = {lin} дБ  (разумный диапазон)")
        chk(-10 <= a   <= 200, f"dB(A)   = {a} дБ  (разумный диапазон)")

    # ── 0x0102 Шаблон 1: все 512 параметров ──────────────────────────────────
    hdr("0x0102 + 0x0201 — Шаблон 1: все 512 параметров")
    mask2 = bytearray(320)
    for bit in range(512):
        wi = bit // 16
        if wi < 160:
            struct.pack_into('<H', mask2, wi*2,
                struct.unpack_from('<H', mask2, wi*2)[0] | (1 << (bit % 16)))
    cmd, data, crc_ok, _ = send(bus, addr, 0x0102, struct.pack('<H', 1) + bytes(mask2))
    chk(cmd == 0x8102, "Шаблон 1 настроен (0x8102)")

    cmd, data, crc_ok, _ = send(bus, addr, 0x0201, struct.pack('<H', 1))
    chk(cmd == 0x8201,      "Ответный код 0x8201")
    chk(len(data) == 1024,  "1024 байта (512 × 2 байта)", f"получено {len(data)}")
    chk(crc_ok,             "CRC верна")
    if data:
        n = len(data) // 2
        inf(f"Получено {n} параметров × 2 байта = {len(data)} байт данных")
        vals = [decode88(struct.unpack_from('<H', data, i*2)[0]) for i in range(5)]
        inf(f"Первые 5 значений: {vals}")

    # ── 0x0208 Все режимы ─────────────────────────────────────────────────────
    hdr("0x0208 — Переключение состояний прибора (10 режимов)")
    modes = [(48,"MODE_CONNECTED"),(49,"MODE_POST_CONNECT"),(50,"MODE_SLM"),
             (51,"MODE_DBCZ"),(52,"MODE_THOCT"),(53,"MODE_DBINF"),
             (54,"MODE_DBULT"),(55,"MODE_STAT"),(56,"MODE_MON"),(57,"MODE_DBAUTO")]
    for code, name in modes:
        cmd, data, crc_ok, _ = send(bus, addr, 0x0208, struct.pack('<H', code))
        chk(cmd == 0x8208, f"Код {code:2d} ({name})")

    # ── 0x0005 Reset ──────────────────────────────────────────────────────────
    hdr("0x0005 — Перезагрузка (RESET)")
    cmd, data, crc_ok, _ = send(bus, addr, 0x0005)
    chk(cmd == 0x8005, "Ответный код 0x8005")
    chk(crc_ok,        "CRC верна")

    # ── 0x0006 Power off ──────────────────────────────────────────────────────
    hdr("0x0006 — Выключение питания")
    cmd, data, crc_ok, _ = send(bus, addr, 0x0006)
    chk(cmd == 0x8006, "Ответный код 0x8006")
    chk(crc_ok,        "CRC верна")

    # ── Ошибка: неизвестная команда ───────────────────────────────────────────
    hdr("Тест ошибки — неизвестная команда 0x7FFF → ожидаем 0xFF07")
    cmd, data, crc_ok, _ = send(bus, addr, 0x7FFF)
    chk(cmd is not None and cmd >= 0xFF00,
        f"Код ошибки 0x{cmd:04X}" if cmd else "Нет ответа")

    # ── Итог ──────────────────────────────────────────────────────────────────
    total = passed + failed
    pct = int(passed / total * 100) if total else 0
    print(f"\n{'═'*55}")
    p = f"{G}{passed} PASSED{RST}"
    f_ = f"{R}{failed} FAILED{RST}"
    print(f"{BOLD}  {p}{BOLD} / {f_}{BOLD} / {total} всего  ({pct}%){RST}")
    print(f"{'═'*55}\n")
    return failed == 0


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    print(f"\n{BOLD}Шумомер UART v2 — полное тестирование протокола{RST}")
    print("Используется виртуальная шина (in-memory queues)\n")

    bus  = VirtualBus()
    stop = threading.Event()

    # Start emulator in background thread
    t = threading.Thread(target=run_emulator, args=(bus, 1, stop), daemon=True)
    t.start()
    time.sleep(0.05)

    try:
        ok_all = run_tests(bus, addr=1)
    finally:
        stop.set()

    sys.exit(0 if ok_all else 1)


if __name__ == "__main__":
    main()
