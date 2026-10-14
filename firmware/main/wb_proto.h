// main/wb_proto.h —— 工牌 <-> PC 中转服务（wb_bridge.py）的双向 BLE 协议。
//
// 两条通道，都在 FF00 服务下：
//   FF01  Write   （PC -> 工牌）  下行帧，首字节 0xAA
//   FF02  Notify  （工牌 -> PC）  上行帧，首字节 0xBB
//
// 帧格式（两边一致）：
//   [0] magic
//   [1] cmd
//   [2] seq      记录索引 / 分片序号；单条帧填 0
//   [3] total    该批次总数；单条帧填 1
//   [4..] payload
//
// 设计取舍：一条记录一帧。BLE 单包在 MTU 247 下可带 ~180 字节有效载荷，
// 足够装下一条对话标题（<=32B）或一条截断消息（<=120B），因此不做跨包重组 ——
// 省掉一整层状态机，也不占额外 RAM（C3 无 PSRAM，堆只有 44KB 富裕）。
#pragma once

#include <stdint.h>
#include <stdbool.h>

// ---- 下行帧类型（PC -> 工牌，写 FF01）----
#define WB_D_MAGIC       0xAA
#define WB_D_STATUS      0x01   // payload: state, flags, upt16, nameLen, name[]
#define WB_D_CONV        0x02   // seq=索引 total=总数; payload: status, titleLen, title[]
#define WB_D_MSG         0x03   // seq=索引 total=总数; payload: role, textLen, text[]
#define WB_D_USAGE       0x04   // payload: ASCII 文本（"12.4 / 30.0 credits"）
#define WB_D_INFO        0x05   // payload: ASCII 文本，模型名 / 提示
#define WB_D_VOICE       0x06   // payload[0]: 0=READY 1=DENY
#define WB_D_SENT        0x07   // payload[0]: 0=OK 1=FAIL, payload[1..]: 文本
#define WB_D_CLEAR       0x08   // payload[0]: 0=全部 1=对话列表 2=消息
#define WB_D_ASR         0x09   // payload[0]: 0=OK 1=FAIL, payload[1..]: 转写文本
                                // 只回显、不发送 —— 等工牌按 OK 确认后才真正投递

// ---- 上行帧类型（工牌 -> PC，Notify FF02）----
#define WB_U_MAGIC       0xBB
#define WB_U_KEY         0x01   // payload: btn, ev
#define WB_U_SELECT      0x02   // payload: convIndex
#define WB_U_REQ         0x03   // payload[0]: WB_REQ_*
#define WB_U_VOICE_START 0x04   // payload: rate16, codec8, rsv8
#define WB_U_VOICE_DATA  0x05   // PCM: payload seq16 + pcm[]
                                // ADPCM: payload seq16, seed16, nibbles[]
#define WB_U_VOICE_END   0x06   // payload: chunks16, samples32
#define WB_U_VOICE_ABORT 0x07   // 无 payload
#define WB_U_SEND        0x08   // payload: convIndex, mode8，请求 PC 转写
                                //   mode=0 转写完直接发；mode=1 先回显等确认
#define WB_U_CONFIRM     0x09   // payload: convIndex，确认发送 PC 缓存的转写文本

// 语音负载编码（WB_U_VOICE_START payload[2]）
// BLE 实测只有 ~10KB/s，8kHz/16bit PCM 要 16KB/s —— 必然丢包。
// ADPCM 4bit/采样 把 8kHz 压到 4KB/s，留出足够余量。
enum {
    WB_VOICE_CODEC_PCM16 = 0,
    WB_VOICE_CODEC_ADPCM = 1,
};

// 刷新请求类型
enum {
    WB_REQ_ALL   = 0,
    WB_REQ_LIST  = 1,
    WB_REQ_MSG   = 2,
    WB_REQ_USAGE = 3,
};

// 语音控制（WB_D_VOICE payload[0]）
enum {
    WB_VOICE_READY = 0,
    WB_VOICE_DENY  = 1,
};

// 语音提交模式（WB_U_SEND payload[1]）
enum {
    WB_SEND_AUTO    = 0,    // 转写完立即投递
    WB_SEND_CONFIRM = 1,    // 先回显转写，等 WB_U_CONFIRM 再投递
};

// 对话状态（WB_D_CONV payload[0]）—— 直接用字符串更省事，但状态用数字省字节
enum {
    WB_CONV_WORKING = 0,
    WB_CONV_DONE    = 1,
    WB_CONV_ERROR   = 2,
};

// 消息角色
enum {
    WB_ROLE_USER = 0,
    WB_ROLE_AI   = 1,
};

// ---- 容量上限 ----
#define WB_MAX_CONV      5      // 只缓存最近 5 条对话（屏幕一屏 5 行，不再翻页）
#define WB_TITLE_LEN     32
#define WB_MAX_MSG       12     // 最多缓存 12 条消息
#define WB_TEXT_LEN      120
#define WB_ASR_LEN       200    // 转写回显文本上限（一屏最多铺满 5 行 14px 字）
#define WB_VOICE_RATE    8000   // 8kHz 单声道 16bit
#define WB_VOICE_CHUNK   128    // 每片 PCM 目标字节数（实际按 MTU 收窄）

typedef struct {
    uint8_t status;                     // WB_CONV_*
    char    title[WB_TITLE_LEN + 1];
} wb_conv_t;

typedef struct {
    uint8_t role;                       // WB_ROLE_*
    char    text[WB_TEXT_LEN + 1];
} wb_msg_t;
