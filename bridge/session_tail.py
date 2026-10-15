#!/usr/bin/env python3
"""打印某个会话 jsonl 里最后 N 条消息（含时间戳），用于验证投递是否真的落库。

用法：python session_tail.py <sessionId> [N] [-f 过滤词]
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path


def find_jsonl(sid):
    for p in Path("~/.workbuddy/projects").expanduser().glob(f"*/{sid}*.jsonl"):
        return p
    return None


def text_of(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        out = []
        for b in content:
            if isinstance(b, dict):
                if b.get("type") == "text":
                    out.append(str(b.get("text") or ""))
                elif b.get("type") == "tool_use":
                    out.append(f"<tool:{b.get('name')}>")
                elif b.get("type") == "tool_result":
                    out.append("<tool_result>")
                elif b.get("type") in ("image", "input_image"):
                    out.append("<image>")
        return " ".join(out)
    return ""


def main():
    sid = sys.argv[1]
    n = int(sys.argv[2]) if len(sys.argv) > 2 else 20
    filt = None
    if "-f" in sys.argv:
        filt = sys.argv[sys.argv.index("-f") + 1]
    p = find_jsonl(sid)
    if not p:
        print(f"找不到会话 {sid}")
        return
    print(f"# {p}")
    rows = []
    with open(p, "r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except Exception:                          # noqa: BLE001
                continue
            t = text_of(d.get("content") if "content" in d else d.get("message", {}).get("content"))
            if filt and filt not in t:
                continue
            ts = d.get("timestamp") or d.get("time") or 0
            try:
                ts = float(ts)
                if ts > 1e12:
                    ts /= 1000
                hhmm = time.strftime("%H:%M:%S", time.localtime(ts))
            except Exception:                          # noqa: BLE001
                hhmm = "??:??:??"
            rows.append((hhmm, d.get("type") or d.get("role") or "?",
                         str(d.get("role") or ""), t.replace("\n", " ")[:110]))
    for hhmm, typ, role, t in rows[-n:]:
        print(f"{hhmm}  {typ:<10} {role:<10} {t}")


if __name__ == "__main__":
    main()
