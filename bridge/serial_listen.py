#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""读取 COM18 串口日志（不复位，纯监听）。"""
import sys
import time

import serial

port = sys.argv[1] if len(sys.argv) > 1 else "COM18"
secs = float(sys.argv[2]) if len(sys.argv) > 2 else 12.0

ser = serial.Serial(port, 115200, timeout=0.3)
print(f"[listen] {port} @115200 for {secs}s (no reset)", flush=True)

end = time.time() + secs
buf = b""
while time.time() < end:
    chunk = ser.read(4096)
    if chunk:
        buf += chunk
ser.close()

text = buf.decode("utf-8", errors="replace")
print(text[-8000:] if text else "(no output)")
