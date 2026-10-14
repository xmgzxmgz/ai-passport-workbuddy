#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ADPCM 编解码一致性测试。

固件把 320 采样（40ms @8kHz）编成一包，每包用上一包的尾采样 reset 预测器。
这里把 wb_adpcm.c 的编码器逐行复刻成 Python，和 wb_bridge.adpcm_decode()
做往返，测信噪比。SNR 正常（语音 >15dB）说明步长表、半字节顺序、
索引更新式在两端是一致口径；跑出 0dB 或负值基本就是半字节顺序或表错了。

用法：python test_adpcm.py
"""
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
from wb_bridge import adpcm_decode  # noqa: E402

STEP = [
    7, 8, 9, 10, 11, 12, 13, 14, 16, 17, 19, 21, 23, 25, 28, 31, 34, 37, 41,
    45, 50, 55, 60, 66, 73, 80, 88, 97, 107, 118, 130, 143, 157, 173, 190, 209,
    230, 253, 279, 307, 337, 371, 408, 449, 494, 544, 598, 658, 724, 796, 876,
    963, 1060, 1166, 1282, 1411, 1552, 1707, 1878, 2066, 2272, 2499, 2749, 3024,
    3327, 3660, 4026, 4428, 4871, 5358, 5894, 6484, 7132, 7845, 8630, 9493,
    10442, 11487, 12635, 13899, 15289, 16818, 18500, 20350, 22385, 24623,
    27086, 29794, 32767,
]
IDX = (-1, -1, -1, -1, 2, 4, 6, 8)


def _step_diff(step, nib):
    d = step >> 3
    if nib & 4:
        d += step
    if nib & 2:
        d += step >> 1
    if nib & 1:
        d += step >> 2
    return d


def encode_block(samples, seed):
    """wb_adpcm.c: wb_adpcm_reset() + wb_adpcm_encode()"""
    pred = int(seed)
    index = 0
    out = bytearray()
    packed = 0
    for i, s in enumerate(samples):
        step = STEP[index]
        diff = int(s) - pred
        sign = 0
        if diff < 0:
            sign = 8
            diff = -diff
        nib = 0
        if diff >= step:
            nib = 4
            diff -= step
        step >>= 1
        if diff >= step:
            nib |= 2
            diff -= step
        step >>= 1
        if diff >= step:
            nib |= 1
        nib |= sign

        vp = _step_diff(STEP[index], nib & 7)
        pred = pred - vp if sign else pred + vp
        pred = -32768 if pred < -32768 else (32767 if pred > 32767 else pred)
        index += IDX[nib & 7]
        index = 0 if index < 0 else (88 if index > 88 else index)

        if (i & 1) == 0:
            packed = nib & 0x0F
        else:
            packed |= (nib & 0x0F) << 4
            out.append(packed)
    if len(samples) & 1:
        out.append(packed)
    return bytes(out)


def floor_pcm(x):
    x = np.clip(np.round(x), -32768, 32767)
    return x.astype(np.int16)


def make_speech(n, rate=8000, rng=None):
    """造一段类语音：1/f 谱（能量集中在低频）+ 3Hz 音节包络。

    刻意不用「几个正弦叠加」那种测试信号 —— 它高频能量太重，
    IMA 的步长自适应跟不上，测出来的 SNR 会明显低于真实语音。
    """
    rng = rng or np.random.default_rng(7)
    w = rng.normal(0, 1, n)
    W = np.fft.rfft(w)
    f = np.fft.rfftfreq(n, 1 / rate)
    W[f > 0] /= f[f > 0] ** 0.9
    W[f < 80] = 0
    sp = np.fft.irfft(W, n)
    sp = sp / np.abs(sp).max()
    t = np.arange(n) / rate
    return sp * (0.35 + 0.65 * np.abs(np.sin(2 * np.pi * 3.0 * t)))


def snr_db(ref, test):
    ref = ref.astype(np.float64)
    err = ref - test.astype(np.float64)
    p_sig = float(np.mean(ref ** 2))
    p_err = float(np.mean(err ** 2))
    if p_err <= 0:
        return float("inf")
    return 10 * np.log10(p_sig / p_err)


def run(block=320, total=8000, drop_pkt=None, label=""):
    """整条流编码 -> 解码 -> 比 SNR。

    drop_pkt 指定丢哪一包。丢包后不能直接和 src 逐点比（会错位），
    所以参照信号也要把那一包切掉 —— 这才是「各包独立可解」的真正含义。
    """
    src = floor_pcm(make_speech(total) * 24000)     # 拉到近满幅
    blocks = [src[o:o + block] for o in range(0, len(src) - block + 1, block)]

    nib = bytearray()
    seeds = []
    seed = 0
    for blk in blocks:
        seeds.append(seed)
        nib += encode_block(blk, seed)
        seed = int(blk[-1])

    per_pkt = block // 2
    dec = bytearray()
    ref = []
    for i, blk in enumerate(blocks):
        if i == drop_pkt:
            continue
        dec += adpcm_decode(nib[i * per_pkt:(i + 1) * per_pkt], seeds[i])
        ref.append(blk)

    ref = np.concatenate(ref)
    got = np.frombuffer(bytes(dec), dtype="<i2")
    n = min(len(ref), len(got))
    v = snr_db(ref[:n], got[:n])
    tag = "无丢包" if drop_pkt is None else f"丢第{drop_pkt}包"
    print(f"[block={block:>3}] {tag}: SNR={v:5.1f}dB  {n} 采样")
    return v


if __name__ == "__main__":
    # 阈值说明：4bit IMA ADPCM 在类语音信号上典型 SNR 是 15~20dB。
    # 低于 15dB 说明表/半字节顺序/索引式跟参考实现不符；接近 0 或负值
    # 就是收发两端口径不一致（解码出来是噪声）。
    ok = True
    for blk, need in ((160, 14.0), (320, 15.0)):
        if not (run(block=blk, total=16000) > need):
            ok = False
    # 丢包不应连锁：各包独立 reset 预测器，丢第 3 包后其余包 SNR 应无变化
    a = run(block=320, total=16000)
    b = run(block=320, total=16000, drop_pkt=3)
    if not (b > 15 and abs(a - b) < 1.0):
        ok = False
        print("  ^^ 丢包后 SNR 劣化，说明包间存在状态耦合")

    # 边界：静音应无损；直流/满幅看稳态误差（IMA 的步长需要几拍才追上）
    for name, sig in (("静音", np.zeros(320)),
                      ("满幅", np.full(320, 32767)),
                      ("满负", np.full(320, -32768))):
        e = encode_block(sig, 0)
        d = np.frombuffer(adpcm_decode(e, 0), dtype="<i2")
        steady = int(np.max(np.abs(sig.astype(int)[32:] - d[32:])))
        print(f"[边界] {name:<4} 编码{len(e)}B -> 解码{len(d)}采样 "
              f"稳态max|err|={steady}")
        if name == "静音" and steady != 0:
            ok = False
    print("RESULT:", "PASS" if ok else "FAIL")
    sys.exit(0 if ok else 1)
