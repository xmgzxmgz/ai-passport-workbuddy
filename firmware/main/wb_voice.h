// main/wb_voice.h —— 麦克风采集（只录不放）。
//
// 采集 8kHz / 16bit / 单声道，边录边通过 BLE Notify 推给 PC。
// 之所以不整段缓冲：工牌是 ESP32-C3 无 PSRAM，BLE 吃掉 67KB 后只剩 ~44KB 空闲堆，
// 而 16kHz 3 秒就要 96KB —— 一缓冲必炸。
#pragma once

#include "esp_err.h"

#include <stdbool.h>
#include <stdint.h>

// 开始采集。返回后采集在独立任务里跑，本函数不阻塞采集过程。
esp_err_t wb_voice_start(void);

// 请求停止采集，并让任务上报 VOICE_END 后自行退出。非阻塞。
void wb_voice_stop(void);

// 采集是否进行中。
bool wb_voice_active(void);

// 已录时长（毫秒）。
uint32_t wb_voice_ms(void);

// 最近一个块的音柱高度 0..15，用于画波形。
uint8_t wb_voice_level(void);

// 累计采样数。
uint32_t wb_voice_samples(void);
