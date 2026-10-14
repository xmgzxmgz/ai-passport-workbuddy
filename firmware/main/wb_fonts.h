#pragma once

// WorkBuddy 中文字库（思源黑体子集，OFL 许可）
//
// 由 fonts/gen_font.js 用 lv_font_conv 生成，覆盖：
//   ASCII 0x20-0x7E + GB2312 全集 6763 汉字 + 常用中文标点（6992 字符）
//
// 选中文字体的原则：
//   含中文的文本（任务标题、对话标题、消息正文）→ wb_16 / wb_14
//   纯英文数字（BUSY / 时间 / 计数）           → 保留 Montserrat（更省 flash）
//
// 注意：字体里没有的字符（emoji、生僻符号）会渲染成豆腐块，
// 中转服务 wb_bridge.py 已做白名单净化，下发前会替换掉。

#include "lvgl.h"

#ifdef __cplusplus
extern "C" {
#endif

extern const lv_font_t lv_font_wb_16;   // 主体：任务标题、列表项
extern const lv_font_t lv_font_wb_14;   // 次要：消息正文、对话标题

#ifdef __cplusplus
}
#endif
