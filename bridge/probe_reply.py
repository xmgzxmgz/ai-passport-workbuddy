#!/usr/bin/env python3
"""投递能力实测：验证 reply 接口在下列条件下的行为。

  A. 正确实例 + 忙碌期
  B. 错误实例（不同窗口的网关）—— 应该 409，且不得污染任何会话
  C. 正确实例 + 空闲期

用法：python probe_reply.py <sessionId> [A|B|C]
"""
from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import gw_discover as G  # noqa: E402


def call(port, path, body=None, pw="", timeout=25):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/api/v1/{path}", data=data,
        headers={"Authorization": f"Bearer {pw}", "X-CodeBuddy-Request": "1",
                 "Content-Type": "application/json"},
        method="POST" if body is not None else "GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", "replace")[:400]
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")[:400]
    except Exception as e:                             # noqa: BLE001
        return -1, f"{type(e).__name__}:{e}"


def jsonl(sid):
    root = Path("~/.workbuddy/projects").expanduser()
    for p in root.glob(f"*/{sid}.jsonl"):
        return p
    return None


def count_hit(path, needle):
    if not path or not path.exists():
        return 0
    n = 0
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            if needle in line:
                n += 1
    return n


def main():
    sid = sys.argv[1]
    stage = (sys.argv[2] if len(sys.argv) > 2 else "A").upper()
    cred = G.find_gateway(skip_self=True)
    pw = cred["password"]
    jl = jsonl(sid)
    print(f"session : {sid}")
    print(f"jsonl   : {jl}")

    # 找到承载该会话的端口（/info.cwd 与心跳 cwd 对齐）
    ports = G.find_gateway_ports(password=pw)
    hb_cwd = None
    for p in Path("~/.workbuddy/sessions").expanduser().glob("*.json"):
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except Exception:                              # noqa: BLE001
            continue
        if d.get("sessionId") == sid and d.get("cwd"):
            hb_cwd = d["cwd"]
    mine, others = None, []
    for p in ports:
        st, body = call(p, "info", pw=pw)
        cwd = ""
        try:
            cwd = json.loads(body).get("data", {}).get("cwd", "")
        except Exception:                              # noqa: BLE001
            pass
        if hb_cwd and cwd == hb_cwd:
            mine = p
        else:
            others.append((p, cwd))
    print(f"心跳 cwd : {hb_cwd}")
    print(f"本会话网关: {mine}")
    for p, c in others:
        print(f"其他实例  : {p} cwd={c}")

    stamp = time.strftime("%H:%M:%S")
    if stage == "A":
        txt = f"[投递实测A-忙碌期 {stamp}] 正确实例注入"
        target = mine
    elif stage == "B":
        txt = f"[投递实测B-错误实例 {stamp}] 不得污染"
        target = others[0][0] if others else None
    else:
        txt = f"[投递实测C-空闲期 {stamp}] 正确实例注入"
        target = mine
    if not target:
        print("!! 没有可用目标端口")
        return

    before = count_hit(jl, txt)
    st, body = call(target, f"sessions/{sid}/reply", {"text": txt}, pw=pw)
    print(f"\nPOST {target}/sessions/{sid}/reply -> {st} {body}")
    for i in range(10):
        time.sleep(1)
        hit = count_hit(jl, txt)
        if hit > before:
            print(f"  ✅ {i+1}s 后落库 (命中 {hit} 次)")
            return
    print("  ❌ 12s 内未落库")

    # 检查是否污染到了别的会话
    root = Path("~/.workbuddy/projects").expanduser()
    for p in root.glob("*/*.jsonl"):
        if p == jl:
            continue
        if count_hit(p, txt):
            print(f"  ⚠️ 污染了 {p.name}")


if __name__ == "__main__":
    main()
