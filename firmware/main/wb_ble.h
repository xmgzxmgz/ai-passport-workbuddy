// main/wb_ble.h —— AI Passport BLE 外设层（双向通道）。
//
// 工牌做外设（Peripheral），PC 侧 wb_bridge.py 做中心（Central）：
//   FF01 Write  ← PC 下发状态 / 对话列表 / 消息 / 额度
//   FF02 Notify → 工牌上报按键 / 选择 / 语音帧 / 发送请求
#pragma once

#include "esp_err.h"
#include "wb_proto.h"

#include <stdbool.h>
#include <stdint.h>

// 状态包（FF01 的 WB_D_STATUS 帧解出的结果）
typedef struct {
    uint8_t  state;       // 0=IDLE 1=BUSY 2=OFFLINE
    bool     online;      // 心跳是否新鲜
    uint16_t uptime_s;    // 忙碌持续秒数
    char     title[33];   // 当前运行对话标题
} wb_status_t;

// ---- 生命周期 ----
esp_err_t wb_ble_init(void);     // 拉起外设并广播；可重复调用
esp_err_t wb_ble_stop(void);     // 断开并释放 NimBLE 资源
bool      wb_ble_is_connected(void);
uint16_t  wb_ble_mtu(void);      // 当前协商出的 ATT MTU（未连接返回 23）

// ---- 下行数据：取用（UI 在 LVGL 上下文里调用）----
bool wb_ble_take_status(wb_status_t *out);        // 有新状态帧返回 true

uint32_t wb_ble_conv_ver(void);                   // 列表版本号，变了才重建 UI
uint8_t  wb_ble_conv_count(void);
bool     wb_ble_conv_get(uint8_t i, wb_conv_t *out);

uint32_t wb_ble_msg_ver(void);                    // 消息列表版本号
uint8_t  wb_ble_msg_count(void);
bool     wb_ble_msg_get(uint8_t i, wb_msg_t *out);
uint16_t wb_ble_msg_total(void);                  // PC 声明的消息总数（可 > 缓存数）

bool     wb_ble_usage_get(char *out, size_t n);   // 额度文本
bool     wb_ble_info_get(char *out, size_t n);    // 模型名 / 提示文本
bool     wb_ble_sent_get(bool *ok, char *out, size_t n);  // 发送结果（取后清标志）
bool     wb_ble_asr_get(bool *ok, char *out, size_t n);   // 转写回显（取后清标志）
bool     wb_ble_voice_granted(void);              // PC 是否已允许录音

// ---- 上行数据：发送 ----
esp_err_t wb_ble_notify(uint8_t cmd, const void *payload, uint16_t plen);
esp_err_t wb_ble_voice_push(const void *pcm, uint16_t len);  // 内部切块，非阻塞
