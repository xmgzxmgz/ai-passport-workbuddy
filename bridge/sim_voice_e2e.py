#!/usr/bin/env python3
"""端到端模拟：真实录音 -> ASR -> 路由 -> 投递 -> 回读验真。

用途：不用工牌，直接用一段真实录音跑完 bridge 的语音投递链路，
把「音频 -> 文字 -> 进对话」每一步的结果都摆出来，便于定位卡在哪。

用法：
    sim_voice_e2e.py <sessionId> [wav路径] [等待上限秒]

不给 wav 就自动挑 voice_out 里最新的一条录音。
它会等目标会话空闲（agent 不忙）再投，所以放进后台跑最合适。
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import wb_bridge as w


def pick_newest_wav() -> Path | None:
    d = w.VOICE_DIR
    if not d.exists():
        return None
    files = sorted(d.glob("*.wav"), key=lambda p: p.stat().st_mtime)
    return files[-1] if files else None


def main() -> int:
    if len(sys.argv) < 2:
        print("用法: sim_voice_e2e.py <sessionId> [wav] [wait_max]")
        return 2
    sid = sys.argv[1]
    wav = Path(sys.argv[2]) if len(sys.argv) > 2 else pick_newest_wav()
    wait_max = float(sys.argv[3]) if len(sys.argv) > 3 else w._BUSY_WAIT_MAX

    print(f"[sim] 目标会话 : {sid}", flush=True)
    print(f"[sim] 录音文件 : {wav}", flush=True)
    if not wav or not wav.exists():
        print("[sim] 找不到录音文件，先用工牌录一条", flush=True)
        return 1
    size = wav.stat().st_size
    rate, dur = 0, 0.0
    try:
        import wave as _wave
        with _wave.open(str(wav), "rb") as fh:
            rate = fh.getframerate()
            dur = fh.getnframes() / rate if rate else 0.0
    except Exception:                                    # noqa: BLE001
        pass
    print(f"[sim] 文件大小 : {size} bytes "
          f"(约 {dur:.2f}s @{rate}Hz)", flush=True)

    # ---- 1. ASR（与工牌完全同一条路径）----
    t0 = time.time()
    text, err = w.transcribe(wav)
    print(f"[sim] 1/4 ASR : {time.time() - t0:.1f}s -> "
          f"{('ERR ' + str(err)) if err else repr(text)}", flush=True)
    if err or not text:
        print("[sim] ASR 没出文字，链路到此为止（这是 ASR 问题，不是投递问题）",
              flush=True)
        return 1

    # ---- 2. 路由（只作展示；投递时 deliver_text 会自己重算并逐个试）----
    try:
        import gw_discover
        cands = gw_discover.deliver_candidates(sid, force=True, verbose=True)
    except Exception as e:                                # noqa: BLE001
        cands = []
        print(f"[sim] 候选发现异常: {type(e).__name__}: {e}", flush=True)
    print(f"[sim] 2/4 路由 : 共 {len(cands)} 个候选", flush=True)
    for c in cands[:4]:
        print(f"[sim]         {c['base']} score={c['score']} [{c['why']}]",
              flush=True)
    if not cands:
        print("[sim] 一个网关实例都没发现 —— 确认 WorkBuddy 正在运行",
              flush=True)
        return 1
    route = (cands[0]["base"], cands[0]["password"])

    # ---- 3. 投递 + 回读验真（deliver_text 内含等空闲/重试/验真）----
    busy = None
    try:
        busy = w._is_session_busy(sid)
    except Exception:                                    # noqa: BLE001
        pass
    print(f"[sim] 3/4 投递 : 当前 busy={busy}，最长等 {wait_max:.0f}s 空闲",
          flush=True)
    t1 = time.time()
    ok, msg, detail = w.deliver_text(
        sid, f"[语音] {text}", how="device", route=route,
        sender_id="aipassport-sim", wait_max=wait_max,
        on_status=lambda s: print(f"[sim]   状态 -> {s}", flush=True))
    elapsed = time.time() - t1

    # ---- 4. 结论 ----
    print(f"[sim] 4/4 结果 : ok={ok} msg={msg!r} detail={detail} "
          f"耗时 {elapsed:.1f}s", flush=True)
    if ok:
        print("[sim] ✅ 端到端成功：录音 -> 文字 -> 已落进对话（屏幕上应出现气泡）",
              flush=True)
    else:
        print("[sim] ❌ 失败：音频与 ASR 都正常，卡在投递环节", flush=True)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
