#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""持续监听 COM18 串口，实时落盘到 serial.log（后台常驻）。"""
import sys
import time

import serial

port = sys.argv[1] if len(sys.argv) > 1 else "COM18"
log_path = sys.argv[2] if len(sys.argv) > 2 else "serial.log"
do_reset = len(sys.argv) > 3 and sys.argv[3] == "reset"

ser = serial.Serial(port, 115200, timeout=0.2)

if do_reset:
    # 通过 DTR/RTS 触发一次硬复位，好把从 bootloader 起的完整启动日志抓下来。
    # 复位丢掉的日志无法事后补齐，所以要在开始记录前就做。
    try:
        ser.setDTR(False)
        ser.setRTS(True)
        time.sleep(0.15)
        ser.setRTS(False)
        time.sleep(0.05)
        print("[monitor] reset pulse sent", flush=True)
    except Exception as e:                          # noqa: BLE001
        print(f"[monitor] reset failed: {e}", flush=True)

print(f"[monitor] {port} @115200 -> {log_path}", flush=True)

def reopen():
    """Windows 的 USB 串口驱动偶发 ERROR_INVALID_FUNCTION，重开即可恢复。"""
    for _ in range(60):
        try:
            s = serial.Serial(port, 115200, timeout=0.2)
            return s
        except Exception as e:                      # noqa: BLE001
            print(f"[monitor] reopen failed: {e}", flush=True)
            time.sleep(1)
    return None


buf = b""
with open(log_path, "a", encoding="utf-8", errors="replace") as fh:
    fh.write(f"\n===== monitor start {time.strftime('%H:%M:%S')} =====\n")
    fh.flush()
    while True:
        try:
            chunk = ser.read(4096)
        except Exception as e:                      # noqa: BLE001
            print(f"[monitor] read error: {e}", flush=True)
            try:
                ser.close()
            except Exception:                       # noqa: BLE001
                pass
            fh.write(f"----- monitor reopen {time.strftime('%H:%M:%S')} -----\n")
            fh.flush()
            ser = reopen()
            if ser is None:
                print("[monitor] giving up", flush=True)
                break
            continue
        if not chunk:
            continue
        buf += chunk
        # 按行切分，保留未完整的尾部
        while b"\n" in buf:
            line, buf = buf.split(b"\n", 1)
            text = line.decode("utf-8", errors="replace").rstrip("\r")
            if text:
                fh.write(text + "\n")
        fh.flush()
try:
    ser.close()
except Exception:                                   # noqa: BLE001
    pass
