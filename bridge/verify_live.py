#!/usr/bin/env python3
"""端到端验收：等目标会话空闲后投一条消息，并回读会话记录验真。

用法：
  python verify_live.py <sessionId> ["文本"] [wait_max]
"""
import sys

import wb_bridge as w

SID = sys.argv[1]
TEXT = sys.argv[2] if len(sys.argv) > 2 else "[语音] 【端到端验收】工牌投递链路已打通"
WAIT = float(sys.argv[3]) if len(sys.argv) > 3 else 900.0

print(f"[verify] sid={SID[:8]} wait_max={WAIT:.0f}s", flush=True)
print(f"[verify] text={TEXT}", flush=True)
print(f"[verify] DB busy now: {w._is_session_busy(SID)}", flush=True)

route = w._route_to_instance(SID, force=True)
print(f"[verify] route={route[0]}", flush=True)

ok, msg, detail = w.deliver_text(
    SID, TEXT, how="device", route=route, sender_id="aipassport-verify",
    wait_max=WAIT, on_status=lambda t: print(f"[verify] status={t}", flush=True))

print(f"[verify] RESULT ok={ok} msg={msg!r} detail={detail}", flush=True)
