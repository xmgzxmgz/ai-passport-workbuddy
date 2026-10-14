#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""BLE 扫描探针：列出周边广播设备，重点找 AI-Passport / FoloToy。"""
import asyncio
import sys

from bleak import BleakScanner


async def main():
    timeout = float(sys.argv[1]) if len(sys.argv) > 1 else 12.0
    print(f"scanning {timeout}s ...", flush=True)
    found = await BleakScanner.discover(timeout=timeout, return_adv=True)

    rows = []
    for addr, (dev, adv) in found.items():
        name = adv.local_name or dev.name or ""
        rows.append((name, addr, adv.rssi, adv.service_uuids or []))

    rows.sort(key=lambda r: (-r[2], r[0]))
    print(f"\n{len(rows)} device(s):\n")
    for name, addr, rssi, uuids in rows:
        mark = "  <== TARGET" if "passport" in name.lower() or "folotoy" in name.lower() else ""
        print(f"{rssi:>5} dBm  {addr:<20} {(name or '(no name)'):<28}{mark}")
        for u in uuids:
            print(f"          svc: {u}")


asyncio.run(main())
