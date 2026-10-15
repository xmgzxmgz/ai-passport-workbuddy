"""ACP 注入探针 v2：按 GUI 真实参数格式向会话投递用户消息。

协议（逆向自 app.asar：内嵌 OpenAPI 的 ACP 章节 + leader-bridge 客户端）：

  1. POST {base}/api/v1/acp/connect            -> {"connectionId","sessionToken"}
  2. POST {base}/api/v1/acp  header: acp-connection-id
     initialize     {"protocolVersion":1,"clientCapabilities":{...}}
  3. session/load   {"sessionId","cwd","mcpServers":[]}   ← mcpServers 必填数组
  4. session/prompt {"sessionId","prompt":[{"type":"text","text":...}]}
  所有响应是 SSE/NDJSON 流：既含 JSON-RPC response，也含
  {"method":"session/update","params":{update:{sessionUpdate:"user_message_chunk"|...}}} 通知。

用法：
  python acp_probe.py scan
  python acp_probe.py send <sid> <cwd> <text...>
"""
from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.request

import gw_discover

CLIENT_CAPABILITIES = {
    "fs": {"readTextFile": True, "writeTextFile": True},
    "terminal": True,
    "sessionCapabilities": {"loadSession": True, "resume": True},
}


def _headers(pw, cid=None):
    h = {"Authorization": f"Bearer {pw}",
         "X-CodeBuddy-Request": "1",
         "Content-Type": "application/json",
         "Accept": "application/json, text/event-stream"}
    if cid:
        h["acp-connection-id"] = cid
    return h


def acp_connect(base, pw, timeout=6):
    req = urllib.request.Request(f"{base}/api/v1/acp/connect", data=b"",
                                 headers=_headers(pw), method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8", "replace"))
    except Exception as e:                                    # noqa: BLE001
        return {"_err": f"{type(e).__name__}: {e}"}


def acp_stream(base, pw, cid, payload, wait_ids=(), max_wait=120.0,
               quiet_after=25.0):
    """POST 一条 JSON-RPC 并把 SSE/NDJSON 流读到满足条件为止。

    返回 (events, responses)：events 是收到的 session/update 通知列表，
    responses 是 {id: result/error}。读到 wait_ids 全部出现后，再等
    quiet_after 秒没新事件就收尾（session/load 的历史回放会持续一段）。
    """
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(f"{base}/api/v1/acp", data=body,
                                headers=_headers(pw, cid), method="POST")
    events, responses = [], {}
    want = set(wait_ids)
    t0 = time.time()
    last = time.time()
    try:
        with urllib.request.urlopen(req, timeout=max_wait) as r:
            for raw in r:
                line = raw.decode("utf-8", "replace").strip()
                if not line:
                    continue
                if line.startswith("data:"):
                    line = line[5:].strip()
                if not line.startswith("{"):
                    continue
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    continue
                last = time.time()
                if "method" in msg:
                    upd = (msg.get("params") or {}).get("update", {})
                    kind = upd.get("sessionUpdate", msg.get("method"))
                    events.append((kind, trunc(upd)))
                elif "id" in msg:
                    responses[msg["id"]] = msg.get("result", msg.get("error"))
                if want and want.issubset(responses.keys()):
                    if time.time() - last > quiet_after:
                        break
                if time.time() - t0 > max_wait:
                    break
    except urllib.error.HTTPError as e:
        return events, {"_http": f"{e.code}: {e.read().decode('utf-8', 'replace')[:200]}"}
    except Exception as e:                                    # noqa: BLE001
        # SSE 流被截断也保留已收到的事件（IncompleteRead 常见）
        responses.setdefault("_stream_end", f"{type(e).__name__}")
    return events, responses


def trunc(d, n=160):
    s = json.dumps(d, ensure_ascii=False)
    return s[:n] + ("…" if len(s) > n else "")


def send(sid, cwd, text):
    cred = gw_discover.find_gateway()
    pw = cred["password"]
    for p in gw_discover.find_gateway_ports(password=pw):
        base = f"http://127.0.0.1:{p}"
        conn = acp_connect(base, pw)
        cid = conn.get("connectionId")
        print(f"[{base}] connect -> {trunc(conn)}")
        if not cid:
            continue
        ev, rp = acp_stream(base, pw, cid, {
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": 1,
                       "clientCapabilities": CLIENT_CAPABILITIES,
                       "clientInfo": {"name": "aipassport", "version": "1.0"}}},
            wait_ids=[1], max_wait=20, quiet_after=2)
        print(f"  initialize -> {trunc(rp.get(1))}")
        ev, rp = acp_stream(base, pw, cid, {
            "jsonrpc": "2.0", "id": 2, "method": "session/load",
            "params": {"sessionId": sid, "cwd": cwd, "mcpServers": []}},
            wait_ids=[2], max_wait=30, quiet_after=4)
        print(f"  session/load -> {trunc(rp.get(2))}  (replayed {len(ev)} events)")
        if "error" in str(rp.get(2)):
            continue
        print("  session/prompt 发送中（本轮结束前流式返回）…")
        ev, rp = acp_stream(base, pw, cid, {
            "jsonrpc": "2.0", "id": 3, "method": "session/prompt",
            "params": {"sessionId": sid,
                       "prompt": [{"type": "text", "text": text}]}},
            wait_ids=[3], max_wait=180, quiet_after=8)
        print(f"  session/prompt -> {trunc(rp.get(3), 300)}")
        kinds = {}
        for k, _ in ev:
            kinds[k] = kinds.get(k, 0) + 1
        print(f"  收到事件 {len(ev)} 条: {kinds}")
        for k, d in ev[:6]:
            print(f"    · {k}: {d[:120]}")
        return


if __name__ == "__main__":
    if len(sys.argv) >= 2 and sys.argv[1] == "scan":
        c = gw_discover.find_gateway()
        for p in gw_discover.find_gateway_ports(password=c["password"]):
            print(p, acp_connect(f"http://127.0.0.1:{p}", c["password"]))
    elif len(sys.argv) >= 5:
        send(sys.argv[2], sys.argv[3], " ".join(sys.argv[4:]))
    else:
        print(__doc__)
