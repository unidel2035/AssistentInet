#!/usr/bin/env python3
"""
Full protocol test — tests ALL commands from UART Protocol v2 spec.
Requires a running emulator on the other end of the serial port pair.

Usage:
    # Terminal 1 (emulator):
    python emulator.py --port /dev/pts/X
    # Terminal 2 (this test):
    python test_protocol.py --port /dev/pts/Y
"""
import argparse
import struct
import sys
import time

import serial

# ── Colors ────────────────────────────────────────────────────────────────────
GREEN  = "\033[92m"
RED    = "\033[91m"
YELLOW = "\033[93m"
BLUE   = "\033[94m"
RESET  = "\033[0m"
BOLD   = "\033[1m"

def ok(msg):   print(f"  {GREEN}✓ PASS{RESET}  {msg}")
def fail(msg): print(f"  {RED}✗ FAIL{RESET}  {msg}")
def info(msg): print(f"  {YELLOW}ℹ INFO{RESET}  {msg}")
def header(msg): print(f"\n{BOLD}{BLUE}{'─'*55}{RESET}\n{BOLD}  {msg}{RESET}\n{'─'*55}")

passed = 0
failed = 0

def result(cond, msg_ok, msg_fail=""):
    global passed, failed
    if cond:
        ok(msg_ok)
        passed += 1
    else:
        fail(msg_fail or msg_ok)
        failed += 1
    return cond

# ── CRC ───────────────────────────────────────────────────────────────────────

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

_pkt_id = 0

def build_packet(addr, command, payload=b'', packet_id=None):
    global _pkt_id
    if packet_id is None:
        _pkt_id = (_pkt_id + 1) & 0xFFFF
        packet_id = _pkt_id
    if payload and len(payload) % 2:
        payload += b'\x00'
    data_len = (len(payload) + 2) if payload else 0
    header_body = struct.pack('<BBHHHHH', 0x7B, addr, command, data_len, packet_id, 0, 0)
    crc_h = crc16(header_body[:10])
    header = header_body[:10] + struct.pack('<H', crc_h)
    if payload:
        return header + payload + struct.pack('<H', crc16(payload))
    return header


def read_response(ser, timeout=3.0):
    """Read full response packet. Returns (command, data_payload) or (None, None)."""
    ser.timeout = timeout
    hdr = ser.read(12)
    if len(hdr) < 12:
        return None, None
    frame, addr, command, data_len, packet_id, reserved = struct.unpack_from('<BBHHHH', hdr, 0)
    crc_h = struct.unpack_from('<H', hdr, 10)[0]

    # Verify header CRC
    expected = crc16(hdr[:10])
    crc_ok = (expected == crc_h)

    if data_len == 0:
        return command, b'', crc_ok, packet_id

    data = ser.read(data_len)
    if len(data) < data_len:
        return command, b'', False, packet_id

    payload = data[:-2]  # strip CRC_data
    crc_d   = struct.unpack_from('<H', data, len(data)-2)[0]
    data_crc_ok = (crc16(payload) == crc_d)

    return command, payload, crc_ok and data_crc_ok, packet_id


def send_cmd(ser, addr, command, payload=b''):
    """Send command and return (resp_command, resp_data, crc_ok)."""
    pkt = build_packet(addr, command, payload)
    ser.reset_input_buffer()
    ser.write(pkt)
    return read_response(ser)


def decode_fract88(word):
    signed = struct.unpack('<h', struct.pack('<H', word & 0xFFFF))[0]
    return round(signed / 256.0, 2)


# ── Test cases ────────────────────────────────────────────────────────────────

def run_tests(ser, addr=1):

    # ── 0x0000 Ping ──────────────────────────────────────────────────────────
    header("0x0000 — Ping")
    cmd, data, crc_ok, pid = send_cmd(ser, addr, 0x0000)
    result(cmd == 0x8000,       f"Ответный код = 0x8000 (получен 0x{cmd:04X})" if cmd else "Нет ответа")
    result(data == b'',         f"Данные отсутствуют (len={len(data)})")
    result(crc_ok,              "CRC заголовка корректна")

    # ── 0x0001 Блокировка клавиатуры ─────────────────────────────────────────
    header("0x0001 — Блокировка клавиатуры")
    cmd, data, crc_ok, _ = send_cmd(ser, addr, 0x0001)
    result(cmd == 0x8001,       f"Ответный код = 0x8001 (получен 0x{cmd:04X})" if cmd else "Нет ответа")
    result(data == b'',         f"Данные отсутствуют (len={len(data)})")
    result(crc_ok,              "CRC корректна")
    info("Клавиатура заблокирована (повторный вызов разблокирует)")

    # ── 0x0002 Чтение версий ──────────────────────────────────────────────────
    header("0x0002 — Чтение версий прибора")
    cmd, data, crc_ok, _ = send_cmd(ser, addr, 0x0002)
    result(cmd == 0x8002,       f"Ответный код = 0x8002 (получен 0x{cmd:04X})" if cmd else "Нет ответа")
    result(len(data) == 6,      f"Длина данных = 6 байт (получено {len(data)})")
    result(crc_ok,              "CRC корректна")
    if len(data) >= 6:
        v_slm = struct.unpack_from('<H', data, 0)[0]
        v_vib = struct.unpack_from('<H', data, 2)[0]
        v_bio = struct.unpack_from('<H', data, 4)[0]
        info(f"Версия шумомера:  {v_slm>>8}.{v_slm&0xFF}")
        info(f"Версия виброметра: {v_vib>>8}.{v_vib&0xFF}")
        info(f"Версия BIOS:       {v_bio>>8}.{v_bio&0xFF}")

    # ── 0x0003 Установка скорости UART ───────────────────────────────────────
    header("0x0003 — Установка скорости UART (115200 → код 0)")
    payload = struct.pack('<H', 0)   # 0 = 115200
    cmd, data, crc_ok, _ = send_cmd(ser, addr, 0x0003, payload)
    result(cmd == 0x8003,       f"Ответный код = 0x8003 (получен 0x{cmd:04X})" if cmd else "Нет ответа")
    result(data == b'',         f"Данные отсутствуют (len={len(data)})")
    result(crc_ok,              "CRC корректна")
    info("Ответ на старой скорости — переключение не применяется в тесте")

    # ── 0x0004 Установка адреса ───────────────────────────────────────────────
    header("0x0004 — Установка нового адреса (тест → 0x01, без изменений)")
    payload = struct.pack('<H', addr)   # set same address — no actual change
    cmd, data, crc_ok, _ = send_cmd(ser, addr, 0x0004, payload)
    result(cmd == 0x8004,       f"Ответный код = 0x8004 (получен 0x{cmd:04X})" if cmd else "Нет ответа")
    result(data == b'',         f"Данные отсутствуют (len={len(data)})")
    result(crc_ok,              "CRC корректна")

    # ── 0x0009 Номер прибора ──────────────────────────────────────────────────
    header("0x0009 — Чтение номера прибора")
    cmd, data, crc_ok, _ = send_cmd(ser, addr, 0x0009)
    result(cmd == 0x8009,       f"Ответный код = 0x8009 (получен 0x{cmd:04X})" if cmd else "Нет ответа")
    result(len(data) == 4,      f"Длина данных = 4 байта (получено {len(data)})")
    result(crc_ok,              "CRC корректна")
    if len(data) >= 4:
        device_id = struct.unpack_from('<I', data, 0)[0]
        info(f"Номер прибора: {device_id}")

    # ── 0x0100 Установка времени ──────────────────────────────────────────────
    header("0x0100 — Установка времени (12:34:56)")
    payload = struct.pack('<HH', (34 << 8) | 56, 12)   # MM=34, SS=56, HH=12
    cmd, data, crc_ok, _ = send_cmd(ser, addr, 0x0100, payload)
    result(cmd == 0x8100,       f"Ответный код = 0x8100 (получен 0x{cmd:04X})" if cmd else "Нет ответа")
    result(data == b'',         f"Данные отсутствуют (len={len(data)})")
    result(crc_ok,              "CRC корректна")
    info("Время 12:34:56 установлено")

    # ── 0x0101 Установка даты ─────────────────────────────────────────────────
    header("0x0101 — Установка даты (23.03.2026)")
    payload = struct.pack('<HH', (3 << 8) | 23, 26)    # MM=03, DD=23, YY=26
    cmd, data, crc_ok, _ = send_cmd(ser, addr, 0x0101, payload)
    result(cmd == 0x8101,       f"Ответный код = 0x8101 (получен 0x{cmd:04X})" if cmd else "Нет ответа")
    result(data == b'',         f"Данные отсутствуют (len={len(data)})")
    result(crc_ok,              "CRC корректна")
    info("Дата 23.03.2026 установлена")

    # ── 0x0102 Настройка шаблона ──────────────────────────────────────────────
    header("0x0102 — Настройка шаблона (биты: dBA RMS=264, dBLin RMS=240)")
    mask = bytearray(320)
    for bit in [240, 264]:
        wi = bit // 16
        struct.pack_into('<H', mask, wi*2,
            struct.unpack_from('<H', mask, wi*2)[0] | (1 << (bit % 16)))
    payload = struct.pack('<H', 0) + bytes(mask)   # template slot 0
    cmd, data, crc_ok, _ = send_cmd(ser, addr, 0x0102, payload)
    result(cmd == 0x8102,       f"Ответный код = 0x8102 (получен 0x{cmd:04X})" if cmd else "Нет ответа")
    result(data == b'',         f"Данные отсутствуют (len={len(data)})")
    result(crc_ok,              "CRC корректна")
    info("Шаблон 0: 2 параметра (Lin RMS + A RMS)")

    # ── 0x0201 Чтение данных по шаблону ──────────────────────────────────────
    header("0x0201 — Чтение данных по шаблону (slot 0)")
    payload = struct.pack('<H', 0)   # template 0
    cmd, data, crc_ok, _ = send_cmd(ser, addr, 0x0201, payload)
    result(cmd == 0x8201,       f"Ответный код = 0x8201 (получен 0x{cmd:04X})" if cmd else "Нет ответа")
    result(len(data) == 4,      f"Длина = 4 байта (2 параметра × 2 байта, получено {len(data)})")
    result(crc_ok,              "CRC корректна")
    if len(data) >= 4:
        lin = decode_fract88(struct.unpack_from('<H', data, 0)[0])
        a   = decode_fract88(struct.unpack_from('<H', data, 2)[0])
        result(-10 <= lin <= 200, f"dB(Lin) RMS = {lin} дБ (диапазон разумный)")
        result(-10 <= a   <= 200, f"dB(A)   RMS = {a}   дБ (диапазон разумный)")

    # Проверка второго шаблона (все параметры)
    header("0x0102 + 0x0201 — Шаблон 1 (все 347 параметров)")
    mask2 = bytearray(320)
    for bit in range(512):
        wi = bit // 16
        if wi < 160:
            struct.pack_into('<H', mask2, wi*2,
                struct.unpack_from('<H', mask2, wi*2)[0] | (1 << (bit % 16)))
    payload2 = struct.pack('<H', 1) + bytes(mask2)
    cmd, data, crc_ok, _ = send_cmd(ser, addr, 0x0102, payload2)
    result(cmd == 0x8102, f"Шаблон 1 настроен (код 0x8102)")

    payload = struct.pack('<H', 1)
    cmd, data, crc_ok, _ = send_cmd(ser, addr, 0x0201, payload)
    result(cmd == 0x8201,       f"Ответный код = 0x8201 (получен 0x{cmd:04X})" if cmd else "Нет ответа")
    result(len(data) > 100,     f"Данных получено {len(data)} байт > 100")
    result(crc_ok,              "CRC корректна")
    if data:
        n = len(data) // 2
        info(f"Получено {n} параметров × 2 байта = {len(data)} байт")
        vals = [decode_fract88(struct.unpack_from('<H', data, i*2)[0]) for i in range(min(n,5))]
        info(f"Первые 5 значений: {vals}")

    # ── 0x0208 Переключение состояний ────────────────────────────────────────
    header("0x0208 — Переключение состояний прибора")
    modes = [
        (50, "MODE_SLM"),
        (51, "MODE_DBCZ"),
        (52, "MODE_THOCT"),
        (56, "MODE_MON"),
        (50, "MODE_SLM (вернуть)"),
    ]
    for code, name in modes:
        payload = struct.pack('<H', code)
        cmd, data, crc_ok, _ = send_cmd(ser, addr, 0x0208, payload)
        result(cmd == 0x8208,   f"Код {code} ({name}) → 0x8208")
        time.sleep(0.05)

    # ── 0x0005 Перезагрузка ───────────────────────────────────────────────────
    header("0x0005 — Перезагрузка (RESET)")
    cmd, data, crc_ok, _ = send_cmd(ser, addr, 0x0005)
    result(cmd == 0x8005,       f"Ответный код = 0x8005 (получен 0x{cmd:04X})" if cmd else "Нет ответа")
    result(crc_ok,              "CRC корректна")

    # ── 0x0006 Выключение питания ─────────────────────────────────────────────
    header("0x0006 — Выключение питания")
    cmd, data, crc_ok, _ = send_cmd(ser, addr, 0x0006)
    result(cmd == 0x8006,       f"Ответный код = 0x8006 (получен 0x{cmd:04X})" if cmd else "Нет ответа")
    result(crc_ok,              "CRC корректна")

    # ── Тест неизвестной команды (ожидаем ошибку) ────────────────────────────
    header("Тест ошибки — неизвестная команда 0x7FFF")
    cmd, data, crc_ok, _ = send_cmd(ser, addr, 0x7FFF)
    result(cmd is not None and cmd >= 0xFF00,
           f"Получен код ошибки 0x{cmd:04X}" if cmd else "Нет ответа",
           f"Ожидался код ошибки >= 0xFF00, получен 0x{cmd:04X}" if cmd else "Нет ответа")

    # ── Итог ──────────────────────────────────────────────────────────────────
    total = passed + failed
    pct   = int(passed / total * 100) if total else 0
    print(f"\n{'═'*55}")
    print(f"{BOLD}  ИТОГО: {GREEN}{passed} PASSED{RESET}{BOLD} / {RED}{failed} FAILED{RESET}{BOLD} / {total} всего  ({pct}%){RESET}")
    print(f"{'═'*55}\n")
    return failed == 0


def main():
    parser = argparse.ArgumentParser(description="Protocol v2 full test")
    parser.add_argument("--port", default="/dev/pts/4", help="Serial port (emulator side B)")
    parser.add_argument("--baud", type=int, default=115200)
    parser.add_argument("--addr", type=int, default=1)
    args = parser.parse_args()

    print(f"\n{BOLD}Шумомер UART v2 — полное тестирование протокола{RESET}")
    print(f"Порт: {args.port}  Скорость: {args.baud}  Адрес: {args.addr}\n")

    try:
        ser = serial.Serial(args.port, args.baud, timeout=3)
    except Exception as e:
        print(f"{RED}Ошибка открытия порта: {e}{RESET}")
        sys.exit(1)

    try:
        ok_all = run_tests(ser, args.addr)
        sys.exit(0 if ok_all else 1)
    finally:
        ser.close()


if __name__ == "__main__":
    main()
