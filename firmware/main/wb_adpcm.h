// main/wb_adpcm.h —— IMA / DVI 4bit ADPCM 编码器。
//
// 为什么需要它：BLE 在 Windows 侧实际只能跑 ~10KB/s（≈43 包/s），
// 而 8kHz/16bit 单声道 PCM 是 16KB/s —— 链路先天不够，必然丢包。
// ADPCM 把每个采样压到 4bit（4:1），8kHz 只要 4KB/s，留出 2.5 倍余量。
//
// 每个包都用上一包的最后一个原始采样值做 seed 重置预测器，
// 所以包与包之间不共享状态 —— 丢一个包只坏那 40ms，不会连锁扩散。
#pragma once

#include <stddef.h>
#include <stdint.h>

typedef struct {
    int32_t pred;      // 预测器（上一采样值）
    int32_t index;     // 步长表索引 0..88
} wb_adpcm_t;

// 用 seed 重置预测器。收发两端必须用同一个 seed 才能对齐。
void wb_adpcm_reset(wb_adpcm_t *st, int16_t seed);

// 把 n 个 int16 编成 n/2 字节：先低半字节，再高半字节（IMA 惯例）。
// n 必须为偶数。
void wb_adpcm_encode(wb_adpcm_t *st, const int16_t *in, size_t n, uint8_t *out);
