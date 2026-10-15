#!/usr/bin/env python3
"""现场地图：把当前所有网关实例、心跳文件、会话归属一次性打出来。

用途：投递失败时先跑这个，看「目标会话 -> 哪个网关端口」到底能不能对上。
    python live_map.py [目标sessionId前缀]
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import gw_discover as G  # noqa: E402


def hb_dump():
    root = Path("~/.workbuddy/sessions").expanduser()
    now = time.time() * 1000
    rows = []
    for p in sorted(root.glob("*.json")):
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except Exception:                              # noqa: BLE001
            continue
        hb = d.get("lastHeartbeat") or d.get("updatedAt") or 0
        rows.append({
            "file": p.name,
            "pid": d.get("pid"),
            "sid": str(d.get("sessionId"))[:8],
            "kind": d.get("kind"),
            "endpoint": d.get("endpoint") or d.get("url") or "-",
            "cwd": str(d.get("cwd") or "")[:52],
            "age_s": round((now - hb) / 1000, 1) if hb else -1,
        })
    return rows


def main():
    prefer = sys.argv[1] if len(sys.argv) > 1 else None
    print("=" * 78)
    print("心跳文件 ~/.workbuddy/sessions/*.json")
    print("=" * 78)
    for r in hb_dump():
        mark = "  <<< 目标" if prefer and r["sid"].startswith(prefer[:8]) else ""
        print(f"  pid={str(r['pid']):>6} sid={r['sid']} kind={str(r['kind']):<12} "
              f"ep={str(r['endpoint']):<24} age={r['age_s']:>7}s {r['cwd']}{mark}")

    print()
    print("=" * 78)
    print("网关实例")
    print("=" * 78)
    cred = G.find_gateway(skip_self=False)
    if not cred:
        print("  自环境凭据不可用，改扫活进程…")
        cred = G.find_gateway(skip_self=True)
    if not cred:
        print("  !! 找不到任何网关凭据")
        return
    pw = cred["password"]
    print(f"  凭据来源 pid={cred['pid']} host={cred['host']} len={len(pw)}")
    ports = G.find_gateway_ports(password=pw)
    print(f"  候选端口: {ports}")
    for p in ports:
        base = f"http://127.0.0.1:{p}"
        st = G.probe_gateway(base, pw)
        live = G._live_session_on(p, pw)
        owner = G._owner_session_on(p, pw)
        print(f"\n  --- {base} ---")
        if st:
            keep = {k: st.get(k) for k in
                    ("status", "activeSessionId", "sessionId", "cwd",
                     "version", "uptime", "senderCount")
                    if k in st}
            print(f"      /status : {json.dumps(keep, ensure_ascii=False)}")
        else:
            print("      /status : <认证失败或非网关>")
        print(f"      live    : {live}")
        print(f"      owner   : {owner}")
        try:
            sess = G and json.loads((__import__("urllib.request", fromlist=["x"])
                                     .urlopen(__import__("urllib.request",
                                                         fromlist=["x"]).Request(
                                         f"{base}/api/v1/sessions",
                                         headers={"Authorization": f"Bearer {pw}",
                                                  "X-CodeBuddy-Request": "1"}),
                                         timeout=1.5)
                                     .read().decode("utf-8", "replace")))
            rows = sess.get("data") or []
            if isinstance(rows, dict):
                rows = rows.get("sessions") or []
            ids = [str(r.get("id") or r.get("sessionId"))[:8] for r in rows[:6]]
            print(f"      sessions: {ids}")
        except Exception as e:                         # noqa: BLE001
            print(f"      sessions: <{type(e).__name__}>")

    print()
    print("=" * 78)
    print("路由判定")
    print("=" * 78)
    if prefer:
        ep = G.endpoint_for_session(prefer)
        print(f"  endpoint_for_session({prefer[:8]}) -> {ep}")
    insts = G.discover_instances(verbose=False, prefer_sid=prefer)
    for i in insts:
        print(f"  {i['base']} live={str(i['live_sid'])[:8]} "
              f"owner={str(i['owner_sid'])[:8]}")


if __name__ == "__main__":
    main()
