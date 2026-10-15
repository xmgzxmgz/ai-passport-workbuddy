#!/usr/bin/env python3
"""决定性探针：会话空闲时，外部能否通过 ACP 注入用户消息？

背景（2026-10-15 实机结论链）：
  * GET /api/v1/sessions/live —— 只有 writer 附着（run 进行中）时非空
  * POST /api/v1/sessions/{id}/reply —— 只接受「当前活会话」，空闲 409
  * ACP session/load —— 会话已有外部 writer 时返回 -32000 writer_occupied

所以"能不能发进去"取决于 **会话此刻有没有 writer**。本探针守在
目标会话从"忙"变"闲"的瞬间发起 ACP 注入，把这条判定测死。

用法：
  python idle_probe.py watch <sessionId> <cwd> [timeout_s]
"""
import json
import sys
import time
import urllib.request

import acp_probe
import gw_discover

H = {"X-CodeBuddy-Request": "1"}


def _get(base, pw, path):
    req = urllib.request.Request(base + path,
                                 headers={"Authorization": "Bearer " + pw, **H})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return json.loads(r.read().decode("utf-8", "replace"))
    except Exception as e:                                    # noqa: BLE001
        return {"_err": f"{type(e).__name__}: {e}"}


def find_instance(sid):
    """找 owner 或 live 等于 sid 的实例（按对话路由）。"""
    for i in gw_discover.discover_instances(prefer_sid=sid):
        if i["live_sid"] == sid or i["owner_sid"] == sid:
            return i
    return None


def busy_state(base, pw, sid):
    """返回 (busy, 说明)。busy=该会话是否被 writer 占着。"""
    st = _get(base, pw, "/api/v1/status")
    lv = _get(base, pw, "/api/v1/sessions/live")
    busy_flag = bool(st.get("busy")) if isinstance(st, dict) else False
    live_sid = lv.get("sessionId") if isinstance(lv, dict) else None
    occ = bool(lv.get("writerOccupied")) if isinstance(lv, dict) else False
    busy = busy_flag or occ or (live_sid == sid)
    return busy, f"status.busy={busy_flag} live={str(live_sid)[:8]} occ={occ}"


def try_inject(base, pw, sid, cwd, text):
    """返回 (阶段, 详情, 事件列表)。"""
    conn = acp_probe.acp_connect(base, pw)
    cid = conn.get("connectionId")
    if not cid:
        return "connect-fail", str(conn)[:200], []
    acp_probe.acp_stream(base, pw, cid,
                         {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                          "params": {"protocolVersion": 1,
                                     "clientCapabilities": acp_probe.CLIENT_CAPABILITIES,
                                     "clientInfo": {"name": "aipassport", "version": "1.0"}}},
                         wait_ids=[1], max_wait=12, quiet_after=2)
    ev, rp = acp_probe.acp_stream(base, pw, cid,
                                  {"jsonrpc": "2.0", "id": 2, "method": "session/load",
                                   "params": {"sessionId": sid, "cwd": cwd, "mcpServers": []}},
                                  wait_ids=[2], max_wait=20, quiet_after=4)
    load = rp.get(2)
    if not isinstance(load, dict) or "result" not in load:
        return "load-fail", json.dumps(load, ensure_ascii=False)[:300], ev
    ev, rp = acp_probe.acp_stream(base, pw, cid,
                                  {"jsonrpc": "2.0", "id": 3, "method": "session/prompt",
                                   "params": {"sessionId": sid,
                                              "prompt": [{"type": "text", "text": text}]}},
                                  wait_ids=[3], max_wait=180, quiet_after=12)
    return "prompt", json.dumps(rp.get(3), ensure_ascii=False)[:400], ev


def watch(sid, cwd, timeout=900):
    print(f"[probe] watch {sid[:8]} cwd={cwd} timeout={timeout}s", flush=True)
    t0 = time.time()
    last = ""
    while time.time() - t0 < timeout:
        cred = gw_discover.find_gateway()
        if not cred:
            time.sleep(3)
            continue
        pw = cred["password"]
        inst = find_instance(sid)
        if not inst:
            last = "no instance"
            time.sleep(3)
            continue
        base = inst["base"]
        busy, why = busy_state(base, pw, sid)
        if busy:
            if why != last:
                print(f"[probe] {base} 忙 -> 等空闲 ({why})", flush=True)
                last = why
            time.sleep(2)
            continue
        print(f"[probe] {base} 空闲！({why}) -> 立即 ACP 注入", flush=True)
        stage, detail, ev = try_inject(
            base, pw, sid, cwd,
            f"【工牌通道验证 {time.strftime('%H:%M:%S')}】外部 ACP 注入测试，"
            f"请回复四个字：通道打通")
        print(f"[probe] 结果阶段={stage}", flush=True)
        print(f"[probe] 详情={detail}", flush=True)
        for k, d in ev:
            if k in ("user_message_chunk", "agent_message_chunk",
                     "session_info_update", "usage_update"):
                print(f"[probe]   ev {k}: {d[:160]}", flush=True)
        return stage
    print("[probe] 超时，未捕捉到空闲窗口", flush=True)
    return "timeout"


if __name__ == "__main__":
    if len(sys.argv) >= 4 and sys.argv[1] == "watch":
        to = int(sys.argv[4]) if len(sys.argv) > 4 else 900
        watch(sys.argv[2], sys.argv[3], to)
    else:
        print(__doc__)
