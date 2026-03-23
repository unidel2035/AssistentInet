#!/usr/bin/env python3
"""
Self-contained test runner.
Creates a virtual PTY pair using Python pty module,
runs emulator in a thread, runs protocol tests against it.
No socat or external tools needed.
"""
import os
import pty
import sys
import threading
import time
import struct
import math
import random

# ── Colors ────────────────────────────────────────────────────────────────────
GREEN  = "\033[92m"; RED  = "\033[91m"
YELLOW = "\033[93m"; BLUE = "\033[94m"
RESET  = "\033[0m";  BOLD = "\033[1m"

def ok(msg):     print(f"  {GREEN}✓ PASS{RESET}  {msg}")
def fail(msg):   print(f"  {RED}✗ FAIL{RESET}  {msg}")
def info(msg):   print(f"  {YELLOW}ℹ{RESET}      {msg}")
def hdr(msg):    print(f"\n{BOLD}{BLUE}── {msg}{RESET}")

passed = 0
failed = 0

def result(cond, label, detail=""):
    global passed, failed
    if cond:
        ok(label + (f"  [{detail}]" if detail else ""))
        passed += 1
    else:
        fail(label + (f"  [{detail}]" if detail else ""))
        failed += 1
    return cond

# ── CRC RFC 1071 ──────────────────────────────────────────────────────────────

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

_pid = 0
def build_packet(addr, cmd, payload=b''):
    global _pid
    _pid = (_pid + 1) & 0xFFFF
    if payload and len(payload) % 2:
        payload += b'\x00'
    data_len = (len(payload) + 2) if payload else 0
    hbody = struct.pack('<BBHHHHH', 0x7B, addr, cmd, data_len, _pid, 0, 0)
    hdr_bytes = hbody[:10] + struct.pack('<H', crc16(hbody[:10]))
    if payload:
        return hdr_bytes + payload + struct.pack('<H', crc16(payload))
    return hdr_bytes

def build_response(addr, cmd, payload=b'', pid=0):
    if payload and len(payload) % 2:
        payload += b'\x00'
    data_len = (len(payload) + 2) if payload else 0
    hbody = struct.pack('<BBHHHHH', 0x7A, addr, cmd, data_len, pid, 0, 0)
    hdr_bytes = hbody[:10] + struct.pack('<H', crc16(hbody[:10]))
    if payload:
        return hdr_bytes + payload + struct.pack('<H', crc16(payload))
    return hdr_bytes

# ── Low-level read helpers ────────────────────────────────────────────────────

def fd_read_exact(fd, n, timeout=3.0):
    """Read exactly n bytes from fd with timeout."""
    buf = b''
    deadline = time.time() + timeout
    while len(buf) < n:
        if time.time() > deadline:
            return None
        try:
            chunk = os.read(fd, n - len(buf))
            if chunk:
                buf += chunk
        except BlockingIOError:
            time.sleep(0.001)
    return buf

def fd_read_packet(fd, timeout=3.0):
    """Read full packet from fd. Returns (command, payload, crc_ok, pid)."""
    hdr = fd_read_exact(fd, 12, timeout)
    if not hdr or len(hdr) < 12:
        return None, None, False, 0
    frame, addr, command, data_len, pid, _ = struct.unpack_from('<BBHHHH', hdr, 0)
    crc_h = struct.unpack_from('<H', hdr, 10)[0]
    crc_ok = (crc16(hdr[:10]) == crc_h)
    if data_len == 0:
        return command, b'', crc_ok, pid
    data = fd_read_exact(fd, data_len, timeout)
    if not data:
        return command, b'', False, pid
    payload   = data[:-2]
    crc_d     = struct.unpack_from('<H', data, len(data)-2)[0]
    data_crc_ok = (crc16(payload) == crc_d)
    return command, payload, crc_ok and data_crc_ok, pid

# ── Emulator (runs in thread) ─────────────────────────────────────────────────

def fract88(v):
    return int(v * 256) & 0xFFFF

def emulator_loop(fd, addr, stop_event):
    """Emulate device responses on file descriptor fd."""
    kb_locked = False
    templates = {}  # slot → num_params
    mode = 50
    buf = b''

    while not stop_event.is_set():
        try:
            chunk = os.read(fd, 256)
            if chunk:
                buf += chunk
        except BlockingIOError:
            time.sleep(0.002)
            continue
        except OSError:
            break

        while len(buf) >= 12:
            frame, _, _, data_len = struct.unpack_from('<BBHH', buf, 0)
            if frame != 0x7B:
                buf = buf[1:]
                continue
            total = 12 + data_len
            if len(buf) < total:
                break
            raw = buf[:total]
            buf = buf[total:]

            _, dev_addr, cmd, dl, pid, _ = struct.unpack_from('<BBHHHH', raw, 0)
            payload = raw[12:12 + dl - 2] if dl > 0 else b''

            if cmd == 0x0000:  # Ping
                os.write(fd, build_response(addr, 0x8000, pid=pid))

            elif cmd == 0x0001:  # Lock keyboard
                kb_locked = not kb_locked
                os.write(fd, build_response(addr, 0x8001, pid=pid))

            elif cmd == 0x0002:  # Read versions
                data = struct.pack('<HHH', 0x0100, 0x0100, 0x0100)
                os.write(fd, build_response(addr, 0x8002, data, pid=pid))

            elif cmd == 0x0003:  # Set baud
                os.write(fd, build_response(addr, 0x8003, pid=pid))

            elif cmd == 0x0004:  # Set address
                os.write(fd, build_response(addr, 0x8004, pid=pid))

            elif cmd == 0x0005:  # Reset
                os.write(fd, build_response(addr, 0x8005, pid=pid))

            elif cmd == 0x0006:  # Power off
                os.write(fd, build_response(addr, 0x8006, pid=pid))

            elif cmd == 0x0009:  # Read device number
                data = struct.pack('<I', 0xDEAD1234)
                os.write(fd, build_response(addr, 0x8009, data, pid=pid))

            elif cmd == 0x0100:  # Set time
                os.write(fd, build_response(addr, 0x8100, pid=pid))

            elif cmd == 0x0101:  # Set date
                os.write(fd, build_response(addr, 0x8101, pid=pid))

            elif cmd == 0x0102:  # Set template
                if len(payload) >= 2:
                    tmpl_id = struct.unpack_from('<H', payload, 0)[0]
                    mask = payload[2:]
                    n = sum(bin(b).count('1') for b in mask)
                    templates[tmpl_id] = n
                os.write(fd, build_response(addr, 0x8102, pid=pid))

            elif cmd == 0x0201:  # Read data
                if len(payload) >= 2:
                    tmpl_id = struct.unpack_from('<H', payload, 0)[0]
                    n = templates.get(tmpl_id, 2)
                    base = 65.0 + 5.0 * math.sin(time.time() / 10)
                    words = [fract88(base + random.uniform(-2, 2)) for _ in range(n)]
                    data = struct.pack(f'<{n}H', *words)
                    os.write(fd, build_response(addr, 0x8201, data, pid=pid))
                else:
                    os.write(fd, build_response(addr, 0xFF11, pid=pid))

            elif cmd == 0x0208:  # Set mode
                if len(payload) >= 2:
                    mode = struct.unpack_from('<H', payload, 0)[0]
                    os.write(fd, build_response(addr, 0x8208, pid=pid))
                else:
                    os.write(fd, build_response(addr, 0xFF1C, pid=pid))

            else:
                os.write(fd, build_response(addr, 0xFF07, pid=pid))

# ── Test suite ────────────────────────────────────────────────────────────────

def run_tests(fd, addr=1):
    def send(cmd, payload=b''):
        pkt = build_packet(addr, cmd, payload)
        os.write(fd, pkt)
        return fd_read_packet(fd)

    def decode_fract88(w):
        s = struct.unpack('<h', struct.pack('<H', w & 0xFFFF))[0]
        return round(s / 256.0, 2)

    # 0x0000 Ping
    hdr("0x0000 — Ping")
    cmd, data, crc_ok, _ = send(0x0000)
    result(cmd == 0x8000, f"Ответный код 0x8000", f"получен 0x{cmd:04X}" if cmd else "нет ответа")
    result(data == b'',   f"Нет данных в ответе")
    result(crc_ok,        f"CRC корректна")

    # 0x0001 Блокировка клавиатуры
    hdr("0x0001 — Блокировка клавиатуры")
    cmd, data, crc_ok, _ = send(0x0001)
    result(cmd == 0x8001, f"Ответный код 0x8001")
    result(data == b'',   f"Нет данных в ответе")
    result(crc_ok,        f"CRC корректна")
    info("Клавиатура заблокирована")

    # 0x0002 Версии
    hdr("0x0002 — Чтение версий прибора")
    cmd, data, crc_ok, _ = send(0x0002)
    result(cmd == 0x8002, f"Ответный код 0x8002")
    result(len(data) == 6, f"Длина данных 6 байт", f"получено {len(data)}")
    result(crc_ok,         f"CRC корректна")
    if len(data) >= 6:
        vs, vv, vb = struct.unpack_from('<HHH', data)
        info(f"Шумомер {vs>>8}.{vs&0xFF} / Виброметр {vv>>8}.{vv&0xFF} / BIOS {vb>>8}.{vb&0xFF}")

    # 0x0003 Скорость UART
    hdr("0x0003 — Установка скорости UART (код 0 = 115200)")
    cmd, data, crc_ok, _ = send(0x0003, struct.pack('<H', 0))
    result(cmd == 0x8003, f"Ответный код 0x8003")
    result(data == b'',   f"Нет данных в ответе")
    result(crc_ok,        f"CRC корректна")

    # 0x0004 Адрес
    hdr("0x0004 — Установка адреса прибора (addr=1)")
    cmd, data, crc_ok, _ = send(0x0004, struct.pack('<H', addr))
    result(cmd == 0x8004, f"Ответный код 0x8004")
    result(crc_ok,        f"CRC корректна")

    # 0x0009 Номер прибора
    hdr("0x0009 — Чтение номера прибора")
    cmd, data, crc_ok, _ = send(0x0009)
    result(cmd == 0x8009, f"Ответный код 0x8009")
    result(len(data) == 4, f"Длина данных 4 байта", f"получено {len(data)}")
    result(crc_ok,         f"CRC корректна")
    if len(data) >= 4:
        dev_id = struct.unpack_from('<I', data, 0)[0]
        info(f"Номер прибора: 0x{dev_id:08X}")

    # 0x0100 Время
    hdr("0x0100 — Установка времени 12:34:56")
    payload = struct.pack('<HH', (34 << 8) | 56, 12)
    cmd, data, crc_ok, _ = send(0x0100, payload)
    result(cmd == 0x8100, f"Ответный код 0x8100")
    result(data == b'',   f"Нет данных в ответе")
    result(crc_ok,        f"CRC корректна")

    # 0x0101 Дата
    hdr("0x0101 — Установка даты 23.03.2026")
    payload = struct.pack('<HH', (3 << 8) | 23, 26)
    cmd, data, crc_ok, _ = send(0x0101, payload)
    result(cmd == 0x8101, f"Ответный код 0x8101")
    result(data == b'',   f"Нет данных в ответе")
    result(crc_ok,        f"CRC корректна")

    # 0x0102 Шаблон — 2 параметра
    hdr("0x0102 — Шаблон 0: dB(Lin) RMS (бит 240) + dB(A) RMS (бит 264)")
    mask = bytearray(320)
    for bit in [240, 264]:
        wi = bit // 16
        struct.pack_into('<H', mask, wi*2,
            struct.unpack_from('<H', mask, wi*2)[0] | (1 << (bit % 16)))
    cmd, data, crc_ok, _ = send(0x0102, struct.pack('<H', 0) + bytes(mask))
    result(cmd == 0x8102, f"Ответный код 0x8102")
    result(data == b'',   f"Нет данных в ответе")
    result(crc_ok,        f"CRC корректна")

    # 0x0201 Читаем по шаблону 0
    hdr("0x0201 — Чтение данных по шаблону 0 (2 параметра)")
    cmd, data, crc_ok, _ = send(0x0201, struct.pack('<H', 0))
    result(cmd == 0x8201, f"Ответный код 0x8201")
    result(len(data) == 4, f"4 байта (2×word fract 8.8)", f"получено {len(data)}")
    result(crc_ok,         f"CRC корректна")
    if len(data) >= 4:
        lin = decode_fract88(struct.unpack_from('<H', data, 0)[0])
        a   = decode_fract88(struct.unpack_from('<H', data, 2)[0])
        result(-10 <= lin <= 200, f"dB(Lin) = {lin} дБ (разумный диапазон)")
        result(-10 <= a   <= 200, f"dB(A)   = {a} дБ (разумный диапазон)")

    # 0x0102 Шаблон 1 — все параметры
    hdr("0x0102 + 0x0201 — Шаблон 1: все 512 параметров")
    mask2 = bytearray(320)
    for bit in range(512):
        wi = bit // 16
        if wi < 160:
            struct.pack_into('<H', mask2, wi*2,
                struct.unpack_from('<H', mask2, wi*2)[0] | (1 << (bit % 16)))
    cmd, data, crc_ok, _ = send(0x0102, struct.pack('<H', 1) + bytes(mask2))
    result(cmd == 0x8102, f"Шаблон 1 настроен")

    cmd, data, crc_ok, _ = send(0x0201, struct.pack('<H', 1))
    result(cmd == 0x8201,    f"Ответный код 0x8201")
    result(len(data) == 1024, f"1024 байта (512 параметров × 2)", f"получено {len(data)}")
    result(crc_ok,            f"CRC корректна")
    if len(data) >= 4:
        n = len(data) // 2
        info(f"Получено {n} параметров × 2 байта = {len(data)} байт")

    # 0x0208 Режимы
    hdr("0x0208 — Переключение состояний прибора")
    modes = [(48,"MODE_CONNECTED"),(49,"MODE_POST_CONNECT"),(50,"MODE_SLM"),
             (51,"MODE_DBCZ"),(52,"MODE_THOCT"),(53,"MODE_DBINF"),
             (54,"MODE_DBULT"),(55,"MODE_STAT"),(56,"MODE_MON"),(57,"MODE_DBAUTO")]
    for code, name in modes:
        cmd, data, crc_ok, _ = send(0x0208, struct.pack('<H', code))
        result(cmd == 0x8208, f"Код {code:2d} ({name})")

    # 0x0005 RESET
    hdr("0x0005 — Перезагрузка (RESET)")
    cmd, data, crc_ok, _ = send(0x0005)
    result(cmd == 0x8005, f"Ответный код 0x8005")
    result(crc_ok,        f"CRC корректна")

    # 0x0006 Выключение
    hdr("0x0006 — Выключение питания")
    cmd, data, crc_ok, _ = send(0x0006)
    result(cmd == 0x8006, f"Ответный код 0x8006")
    result(crc_ok,        f"CRC корректна")

    # Неизвестная команда → ошибка
    hdr("Ошибка — неизвестная команда 0x7FFF (ожидаем 0xFF07)")
    cmd, data, crc_ok, _ = send(0x7FFF)
    result(cmd is not None and cmd >= 0xFF00,
           f"Код ошибки 0x{cmd:04X} >= 0xFF00" if cmd else "Нет ответа")

    # Итог
    total = passed + failed
    pct = int(passed / total * 100) if total else 0
    print(f"\n{'═'*55}")
    print(f"{BOLD}  ИТОГО: {GREEN}{passed} PASSED{RESET}{BOLD} / "
          f"{RED}{failed} FAILED{RESET}{BOLD} / {total} всего  ({pct}%){RESET}")
    print(f"{'═'*55}\n")


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    print(f"\n{BOLD}Шумомер UART v2 — полное тестирование протокола{RESET}")
    print("Создаю виртуальный PTY-порт...\n")

    # Create PTY pair: master=emulator, slave=test
    master_fd, slave_fd = pty.openpty()
    slave_path = os.ttyname(slave_fd)
    info(f"Виртуальный порт: {slave_path}")

    # Make fds non-blocking
    import fcntl
    fcntl.fcntl(master_fd, fcntl.F_SETFL, fcntl.fcntl(master_fd, fcntl.F_GETFL) | os.O_NONBLOCK)
    fcntl.fcntl(slave_fd,  fcntl.F_SETFL, fcntl.fcntl(slave_fd,  fcntl.F_GETFL) | os.O_NONBLOCK)

    # Start emulator thread on master
    stop = threading.Event()
    t = threading.Thread(target=emulator_loop, args=(master_fd, 1, stop), daemon=True)
    t.start()
    time.sleep(0.1)

    # Open slave as serial port and run tests
    import serial
    ser_fd = slave_fd  # we'll use raw fd directly (both ends are our own)

    run_tests(master_fd if False else slave_fd, addr=1)

    stop.set()
    os.close(master_fd)
    os.close(slave_fd)


if __name__ == "__main__":
    main()
