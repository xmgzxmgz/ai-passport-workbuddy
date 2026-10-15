// main/wb_app.c —— WorkBuddy 随身终端界面（英文，深色仪表盘）。
//
// 四屏：
//   PAGE_HOME    状态灯 / 运行中对话 / 计时 / 模型 / 额度
//   PAGE_LIST    对话列表（可滚动选择）
//   PAGE_DETAIL  选中对话的消息流 + 录音入口
//   PAGE_RECORD  录音 → 上传 → PC 转写 → 发送
//
// 按键语义（全部单击；页面内长按 OK 会被 main.c 当成"退出到菜单"，故不依赖）：
//   HOME    OK=进列表   UP=切亮度   DOWN=强制刷新
//   LIST    OK=打开     UP/DOWN=移动（在首项再 UP 回 HOME）
//   DETAIL  OK=开始录音 UP/DOWN=翻页（在首页再 UP 回 LIST）
//   RECORD  OK=停止并发送   UP=取消
//
// 视觉刻意从简：不用像素风装饰（云/草坡/砖纹会吃掉半屏且费 LVGL 池），
// 全部走卡片 + 文字，信息密度优先。
#include "wb_app.h"

#include "wb_ble.h"
#include "wb_proto.h"
#include "wb_voice.h"
#include "wb_fonts.h"

#include "bsp_battery.h"
#include "bsp_display.h"

#include "esp_log.h"
#include "esp_system.h"
#include "esp_timer.h"
#include "lvgl.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>

static const char *TAG = "wb_app";

// ---- 配色（深色）----
#define C_BG     0x080D16
#define C_CARD   0x131C2B
#define C_CARD2  0x1E2A3D
#define C_LINE   0x243348
#define C_TEXT   0xE8EFF9
#define C_DIM    0x7C8FA8
#define C_ACCENT 0x38BDF8
#define C_OK     0x2DD4A7
#define C_WARN   0xF5A524
#define C_ERR    0xF04438

// 对话气泡：自己说的偏青蓝，WorkBuddy 说的用中性卡片色
#define C_BUB_ME 0x17394F
#define C_BUB_AI 0x18222F

#define SCR_W 240
#define SCR_H 320

#define IDLE_DIM_MS   30000
#define IDLE_OFF_MS   60000
#define LINK_TIMEOUT_MS 6000    // 超过这么久没收到状态帧，判 PC 侧不可用

#define LIST_ROWS     5
#define MSG_SLOTS     10
#define BAR_COUNT     18

static const uint32_t DOT_COLOR[3] = { C_OK, C_WARN, C_DIM };
static const char    *STATE_TEXT[3] = { "IDLE", "BUSY", "OFFLINE" };
static const uint8_t  BRIGHT[3] = { 100, 55, 15 };

typedef enum { PAGE_HOME = 0, PAGE_LIST, PAGE_DETAIL, PAGE_RECORD } wb_page_t;

// 录音页状态机：
//   IDLE -> RECORDING -> (发 WB_U_SEND mode=1) -> TRANSCRIBING
//        -> 收到 WB_D_ASR -> CONFIRM --OK--> SENDING -> DONE
//                                  \--UP--> 回 DETAIL
typedef enum {
    REC_IDLE = 0,
    REC_RECORDING,
    REC_TRANSCRIBING,
    REC_CONFIRM,
    REC_SENDING,
    REC_DONE,
} rec_state_t;

static wb_page_t s_page = PAGE_HOME;
static uint8_t   s_bright_idx;
static bool      s_dimmed;
static bool      s_off;              // 已全灭：守卫位，避免每个 tick 重复写背光寄存器
static uint32_t  s_last_active;

static wb_status_t s_st;
static bool        s_have_data;
// 最近一次收到状态帧的时刻。离线的判据在设备侧：BLE 没连 或 超时没帧。
// 不能只信 PC 下发的 state —— PC 侧 `sessions/*.json` 心跳文件常过期，
// 单看它会一直误报 OFFLINE，而实际上中转服务活得好好的。
static uint32_t    s_last_frame;

static lv_timer_t *s_tick;
static lv_timer_t *s_wave;

// ---- HOME ----
static lv_obj_t *s_home;
static lv_obj_t *s_h_dot;
static lv_obj_t *s_h_state;
static lv_obj_t *s_h_link;
static lv_obj_t *s_h_task;
static lv_obj_t *s_h_model;
static lv_obj_t *s_h_time;
static lv_obj_t *s_h_cred;
static lv_obj_t *s_h_bar;

// ---- LIST ----
static lv_obj_t *s_list;
static lv_obj_t *s_l_hdr;
static lv_obj_t *s_l_row[LIST_ROWS];
static lv_obj_t *s_l_dot[LIST_ROWS];
static lv_obj_t *s_l_txt[LIST_ROWS];
static int       s_sel;
static int       s_top;
static uint32_t  s_conv_ver_ui;

// ---- DETAIL ----
// 每条消息两级结构：row（满宽，负责左右对齐）+ bubble（宽度随文本伸缩）
static lv_obj_t *s_detail;
static lv_obj_t *s_d_hdr;
static lv_obj_t *s_d_body;
static lv_obj_t *s_d_row[MSG_SLOTS];
static lv_obj_t *s_d_bub[MSG_SLOTS];
static lv_obj_t *s_d_role[MSG_SLOTS];
static lv_obj_t *s_d_text[MSG_SLOTS];
static int       s_detail_idx;      // 选中的对话索引
static int       s_msg_page;
static uint32_t  s_msg_ver_ui;

// ---- RECORD ----
static lv_obj_t *s_rec;
static lv_obj_t *s_r_dot;
static lv_obj_t *s_r_title;
static lv_obj_t *s_r_time;
static lv_obj_t *s_r_hint;
static lv_obj_t *s_r_sub;
static lv_obj_t *s_r_wave;                 // 波形卡片（录音态）
static lv_obj_t *s_r_heard;                // 转写回显卡片（确认态）
static lv_obj_t *s_r_heard_txt;
static lv_obj_t *s_r_bar[BAR_COUNT];
static rec_state_t s_rec_state = REC_IDLE;
static uint8_t     s_bar_next;
static uint32_t    s_done_at;
static char        s_asr_buf[WB_ASR_LEN + 1];

// ---------------------------------------------------------------------------
// 小工具
// ---------------------------------------------------------------------------

static uint32_t now_ms(void)
{
    return (uint32_t)(esp_timer_get_time() / 1000);
}

static void mark_active(void)
{
    s_last_active = now_ms();
    if (s_off) {
        s_off = false;
        s_dimmed = false;
        bsp_display_backlight(BRIGHT[s_bright_idx]);
    } else if (s_dimmed) {
        s_dimmed = false;
        bsp_display_backlight(BRIGHT[s_bright_idx]);
    }
}

static lv_obj_t *screen_new(void)
{
    lv_obj_t *s = lv_obj_create(NULL);
    lv_obj_remove_style_all(s);
    lv_obj_set_style_bg_color(s, lv_color_hex(C_BG), 0);
    lv_obj_set_style_bg_opa(s, LV_OPA_COVER, 0);
    lv_obj_set_style_pad_all(s, 0, 0);
    lv_obj_clear_flag(s, LV_OBJ_FLAG_SCROLLABLE);
    return s;
}

static lv_obj_t *card_new(lv_obj_t *parent, int x, int y, int w, int h)
{
    lv_obj_t *o = lv_obj_create(parent);
    lv_obj_remove_style_all(o);
    lv_obj_set_pos(o, x, y);
    lv_obj_set_size(o, w, h);
    lv_obj_set_style_bg_color(o, lv_color_hex(C_CARD), 0);
    lv_obj_set_style_bg_opa(o, LV_OPA_COVER, 0);
    lv_obj_set_style_radius(o, 6, 0);
    lv_obj_set_style_pad_all(o, 0, 0);
    lv_obj_clear_flag(o, LV_OBJ_FLAG_SCROLLABLE);
    return o;
}

static lv_obj_t *label_new(lv_obj_t *parent, const char *txt,
                           const lv_font_t *font, uint32_t color)
{
    lv_obj_t *l = lv_label_create(parent);
    lv_label_set_text(l, txt);
    lv_obj_set_style_text_font(l, font, 0);
    lv_obj_set_style_text_color(l, lv_color_hex(color), 0);
    return l;
}

static void fmt_clock(char *out, size_t n, uint32_t sec)
{
    if (sec >= 3600) {
        snprintf(out, n, "%u:%02u:%02u", (unsigned)(sec / 3600),
                 (unsigned)((sec / 60) % 60), (unsigned)(sec % 60));
    } else {
        snprintf(out, n, "%02u:%02u", (unsigned)(sec / 60), (unsigned)(sec % 60));
    }
}

// 估算一段 UTF-8 文本在 14px 字号下的像素宽度。
// 汉字/全角按 14px，ASCII 按 7px —— 只用于给气泡定宽，误差 1~2px 无妨。
static int text_px_width(const char *s)
{
    int w = 0;
    const uint8_t *p = (const uint8_t *)s;
    while (*p) {
        if (*p < 0x80) {
            w += 7;
            p++;
        } else {
            int len = (*p >= 0xF0) ? 4 : ((*p >= 0xE0) ? 3 : 2);
            for (int k = 1; k < len; k++) {
                if (!p[k]) { len = k; break; }   // 截断的非法序列，别越读
            }
            w += 14;
            p += len;
        }
    }
    return w;
}

// 气泡宽度：随文本伸缩，夹在 [76, SCR_W-44] 之间。
// 上限 196 保证左右各有 ~22px 留白，不会被屏幕边缘切掉。
#define BUB_PAD   14            // 气泡左右 padding 7+7
#define BUB_MIN   76
#define BUB_MAX   (SCR_W - 44)

static int bubble_width(const char *s)
{
    int w = text_px_width(s) + BUB_PAD;
    if (w > BUB_MAX) w = BUB_MAX;
    if (w < BUB_MIN) w = BUB_MIN;
    return w;
}

// 向上行请求数据：what = WB_REQ_*，arg = 页码（消息页用）
static void req_data(uint8_t what, uint8_t arg)
{
    uint8_t rq[2] = { what, arg };
    wb_ble_notify(WB_U_REQ, rq, sizeof(rq));
}

// ---------------------------------------------------------------------------
// HOME
// ---------------------------------------------------------------------------

static void home_build(void)
{
    s_home = screen_new();

    // 顶栏：状态点 + 状态字 + 连接/电量
    lv_obj_t *top = card_new(s_home, 8, 6, SCR_W - 16, 40);
    lv_obj_set_style_radius(top, 6, 0);

    s_h_dot = lv_obj_create(top);
    lv_obj_remove_style_all(s_h_dot);
    lv_obj_set_size(s_h_dot, 14, 14);
    lv_obj_set_pos(s_h_dot, 10, 13);
    lv_obj_set_style_radius(s_h_dot, LV_RADIUS_CIRCLE, 0);
    lv_obj_set_style_bg_opa(s_h_dot, LV_OPA_COVER, 0);
    lv_obj_set_style_bg_color(s_h_dot, lv_color_hex(C_DIM), 0);

    s_h_state = label_new(top, "WAITING", &lv_font_montserrat_20, C_TEXT);
    lv_obj_set_pos(s_h_state, 32, 8);

    s_h_link = label_new(top, "BLE --", &lv_font_montserrat_12, C_DIM);
    lv_obj_align(s_h_link, LV_ALIGN_RIGHT_MID, -10, 0);

    // RUNNING 卡片
    // 注意：任务标题用 wb_16（line_height=20，比 Montserrat 14 高 3px），
    // 卡片与下方模型名坐标要跟着让开，否则两行文字会贴死。
    lv_obj_t *run = card_new(s_home, 8, 52, SCR_W - 16, 74);
    label_new(run, "RUNNING", &lv_font_montserrat_12, C_DIM);
    lv_obj_set_pos(lv_obj_get_child(run, 0), 10, 8);

    s_h_task = label_new(run, "--", &lv_font_wb_16, C_TEXT);
    lv_obj_set_pos(s_h_task, 10, 26);
    lv_obj_set_width(s_h_task, SCR_W - 40);
    // DOT：任务名超长显示省略号。原来 SCROLL_CIRCULAR 常驻滚动动画
    // 每帧都要重绘整行 —— 无 PSRAM 的 C3 上这是无谓的 CPU/帧预算消耗。
    lv_label_set_long_mode(s_h_task, LV_LABEL_LONG_DOT);

    s_h_model = label_new(run, "--", &lv_font_montserrat_12, C_ACCENT);
    lv_obj_set_pos(s_h_model, 10, 52);

    // ELAPSED 卡片
    lv_obj_t *el = card_new(s_home, 8, 132, SCR_W - 16, 66);
    label_new(el, "ELAPSED", &lv_font_montserrat_12, C_DIM);
    lv_obj_set_pos(lv_obj_get_child(el, 0), 10, 8);

    s_h_time = label_new(el, "00:00", &lv_font_montserrat_28, C_TEXT);
    lv_obj_align(s_h_time, LV_ALIGN_BOTTOM_MID, 0, -6);

    // CREDITS 卡片
    lv_obj_t *cr = card_new(s_home, 8, 204, SCR_W - 16, 66);
    label_new(cr, "CREDITS", &lv_font_montserrat_12, C_DIM);
    lv_obj_set_pos(lv_obj_get_child(cr, 0), 10, 8);

    s_h_cred = label_new(cr, "--", &lv_font_montserrat_14, C_TEXT);
    lv_obj_set_pos(s_h_cred, 10, 24);

    s_h_bar = lv_bar_create(cr);
    lv_obj_remove_style_all(s_h_bar);
    lv_obj_set_size(s_h_bar, SCR_W - 36, 10);
    lv_obj_set_pos(s_h_bar, 10, 46);
    lv_obj_set_style_radius(s_h_bar, 5, 0);
    lv_obj_set_style_bg_color(s_h_bar, lv_color_hex(C_CARD2), LV_PART_MAIN);
    lv_obj_set_style_bg_opa(s_h_bar, LV_OPA_COVER, LV_PART_MAIN);
    lv_obj_set_style_radius(s_h_bar, 5, LV_PART_INDICATOR);
    lv_obj_set_style_bg_color(s_h_bar, lv_color_hex(C_ACCENT), LV_PART_INDICATOR);
    lv_obj_set_style_bg_opa(s_h_bar, LV_OPA_COVER, LV_PART_INDICATOR);
    lv_bar_set_range(s_h_bar, 0, 100);
    lv_bar_set_value(s_h_bar, 0, LV_ANIM_OFF);

    lv_obj_t *hint = label_new(s_home, "OK List    UP Light    DOWN Refresh",
                               &lv_font_montserrat_12, C_DIM);
    lv_obj_align(hint, LV_ALIGN_BOTTOM_MID, 0, -10);
}

static void home_refresh(void)
{
    // 三态判据（优先级从上到下）：
    //   链路断/超时  → OFFLINE   （PC 侧中转服务没在推，任何 state 都不可信）
    //   已连但没数据 → LINKED    （刚连上，首帧还没到）
    //   有数据       → 用 PC 给的 BUSY / IDLE
    bool linked = wb_ble_is_connected() &&
                  s_last_frame != 0 &&
                  (now_ms() - s_last_frame) < LINK_TIMEOUT_MS;

    if (!linked) {
        lv_obj_set_style_bg_color(s_h_dot, lv_color_hex(DOT_COLOR[2]), 0);
        lv_label_set_text(s_h_state, wb_ble_is_connected() ? "NO DATA" : STATE_TEXT[2]);
    } else if (!s_have_data) {
        lv_obj_set_style_bg_color(s_h_dot, lv_color_hex(C_ACCENT), 0);
        lv_label_set_text(s_h_state, "LINKED");
    } else {
        uint8_t st = s_st.state < 3 ? s_st.state : 2;
        if (st == 2) st = 0;               // 服务在推帧 = 在线，PC 不会再报 OFFLINE
        lv_obj_set_style_bg_color(s_h_dot, lv_color_hex(DOT_COLOR[st]), 0);
        lv_label_set_text(s_h_state, STATE_TEXT[st]);
    }

    lv_label_set_text(s_h_task, (s_have_data && s_st.title[0]) ? s_st.title : "--");

    char tb[16];
    fmt_clock(tb, sizeof(tb), s_have_data ? s_st.uptime_s : 0);
    lv_label_set_text(s_h_time, tb);

    char tmp[24];
    int soc = bsp_battery_soc();
    if (soc < 0) {
        snprintf(tmp, sizeof(tmp), "%s", wb_ble_is_connected() ? "BLE OK" : "BLE --");
    } else {
        snprintf(tmp, sizeof(tmp), "%s %d%%", wb_ble_is_connected() ? "BLE" : "---", soc);
    }
    lv_label_set_text(s_h_link, tmp);

    char info[28];
    if (wb_ble_info_get(info, sizeof(info))) {
        lv_label_set_text(s_h_model, info);
    }

    char usage[52];
    if (wb_ble_usage_get(usage, sizeof(usage))) {
        // PC 侧格式: "<used_x10>|<total_x10>"，如 "124|300" = 12.4 / 30.0
        const char *bar = strchr(usage, '|');
        if (bar) {
            int used = atoi(usage);
            int tot = atoi(bar + 1);
            lv_label_set_text_fmt(s_h_cred, "%d.%d / %d.%d",
                                  used / 10, used % 10, tot / 10, tot % 10);
            if (tot > 0) {
                int pct = (int)((long)used * 100 / tot);
                if (pct > 100) pct = 100;
                lv_bar_set_value(s_h_bar, pct, LV_ANIM_OFF);
            }
        } else {
            lv_label_set_text(s_h_cred, usage);
        }
    } else {
        lv_label_set_text(s_h_cred, "--");
    }
}

// ---------------------------------------------------------------------------
// LIST
// ---------------------------------------------------------------------------

static void list_build(void)
{
    s_list = screen_new();

    lv_obj_t *hdr = card_new(s_list, 8, 6, SCR_W - 16, 34);
    s_l_hdr = label_new(hdr, "CONVERSATIONS", &lv_font_montserrat_12, C_DIM);
    lv_obj_align(s_l_hdr, LV_ALIGN_LEFT_MID, 2, 0);

    for (int i = 0; i < LIST_ROWS; i++) {
        int y = 46 + i * 44;
        lv_obj_t *r = card_new(s_list, 8, y, SCR_W - 16, 40);
        s_l_row[i] = r;

        s_l_dot[i] = lv_obj_create(r);
        lv_obj_remove_style_all(s_l_dot[i]);
        lv_obj_set_size(s_l_dot[i], 8, 8);
        lv_obj_set_pos(s_l_dot[i], 12, 16);
        lv_obj_set_style_radius(s_l_dot[i], LV_RADIUS_CIRCLE, 0);
        lv_obj_set_style_bg_opa(s_l_dot[i], LV_OPA_COVER, 0);
        lv_obj_set_style_bg_color(s_l_dot[i], lv_color_hex(C_DIM), 0);

        s_l_txt[i] = label_new(r, "--", &lv_font_wb_14, C_TEXT);
        lv_obj_set_pos(s_l_txt[i], 28, 11);
        lv_obj_set_width(s_l_txt[i], SCR_W - 56);
        lv_label_set_long_mode(s_l_txt[i], LV_LABEL_LONG_DOT);
    }

    lv_obj_t *hint = label_new(s_list, "OK Open    UP/DN Move    UP@top Home",
                               &lv_font_montserrat_12, C_DIM);
    lv_obj_align(hint, LV_ALIGN_BOTTOM_MID, 0, -10);
}

static void list_refresh(void)
{
    int total = wb_ble_conv_count();
    if (total <= 0) {
        lv_label_set_text_fmt(s_l_hdr, "CONVERSATIONS  (waiting)");
        for (int i = 0; i < LIST_ROWS; i++) {
            lv_label_set_text(s_l_txt[i], "--");
            lv_obj_set_style_bg_color(s_l_row[i], lv_color_hex(C_CARD), 0);
            lv_obj_set_style_bg_color(s_l_dot[i], lv_color_hex(C_DIM), 0);
        }
        return;
    }

    if (s_sel >= total) s_sel = total - 1;
    if (s_sel < 0) s_sel = 0;
    if (s_sel < s_top) s_top = s_sel;
    if (s_sel >= s_top + LIST_ROWS) s_top = s_sel - LIST_ROWS + 1;

    lv_label_set_text_fmt(s_l_hdr, "CONVERSATIONS  %d/%d", s_sel + 1, total);

    for (int i = 0; i < LIST_ROWS; i++) {
        int idx = s_top + i;
        wb_conv_t c;
        if (idx < total && wb_ble_conv_get((uint8_t)idx, &c)) {
            lv_label_set_text(s_l_txt[i], c.title[0] ? c.title : "(untitled)");
            uint32_t col = c.status == WB_CONV_WORKING ? C_WARN
                         : c.status == WB_CONV_ERROR   ? C_ERR : C_OK;
            lv_obj_set_style_bg_color(s_l_dot[i], lv_color_hex(col), 0);
            bool sel = (idx == s_sel);
            lv_obj_set_style_bg_color(s_l_row[i],
                                      lv_color_hex(sel ? C_CARD2 : C_CARD), 0);
            lv_obj_set_style_text_color(s_l_txt[i],
                                        lv_color_hex(sel ? C_ACCENT : C_TEXT), 0);
        } else {
            lv_label_set_text(s_l_txt[i], "");
            lv_obj_set_style_bg_color(s_l_row[i], lv_color_hex(C_CARD), 0);
            lv_obj_set_style_bg_color(s_l_dot[i], lv_color_hex(C_CARD), 0);
        }
    }
}

// ---------------------------------------------------------------------------
// DETAIL
// ---------------------------------------------------------------------------

static void detail_build(void)
{
    s_detail = screen_new();

    lv_obj_t *hdr = card_new(s_detail, 8, 6, SCR_W - 16, 34);
    s_d_hdr = label_new(hdr, "CONVERSATION", &lv_font_wb_14, C_TEXT);
    lv_obj_set_width(s_d_hdr, SCR_W - 30);
    lv_obj_align(s_d_hdr, LV_ALIGN_LEFT_MID, 2, 0);
    lv_label_set_long_mode(s_d_hdr, LV_LABEL_LONG_DOT);

    // 消息流：底部留 12px，避免最后一条被下方的按键提示压住
    s_d_body = lv_obj_create(s_detail);
    lv_obj_remove_style_all(s_d_body);
    lv_obj_set_pos(s_d_body, 0, 46);
    lv_obj_set_size(s_d_body, SCR_W, 228);
    lv_obj_set_style_pad_left(s_d_body, 10, 0);
    lv_obj_set_style_pad_right(s_d_body, 10, 0);
    lv_obj_set_style_pad_bottom(s_d_body, 12, 0);
    lv_obj_set_style_bg_opa(s_d_body, LV_OPA_TRANSP, 0);
    lv_obj_set_scrollbar_mode(s_d_body, LV_SCROLLBAR_MODE_OFF);
    lv_obj_set_flex_flow(s_d_body, LV_FLEX_FLOW_COLUMN);
    lv_obj_set_style_pad_row(s_d_body, 8, 0);

    // 每条消息两段式：row 满宽管左右对齐，bubble 随文本伸缩。
    // bubble 内部必须显式 flex column + pad_row —— 否则 role 与正文两个
    // label 都落在左上角原点互相压住，就是之前"内容被遮挡"的原因。
    for (int i = 0; i < MSG_SLOTS; i++) {
        lv_obj_t *row = lv_obj_create(s_d_body);
        lv_obj_remove_style_all(row);
        lv_obj_set_width(row, LV_PCT(100));
        lv_obj_set_height(row, LV_SIZE_CONTENT);
        lv_obj_set_style_bg_opa(row, LV_OPA_TRANSP, 0);
        lv_obj_set_style_pad_all(row, 0, 0);
        lv_obj_clear_flag(row, LV_OBJ_FLAG_SCROLLABLE);
        lv_obj_set_flex_flow(row, LV_FLEX_FLOW_ROW);
        lv_obj_set_flex_align(row, LV_FLEX_ALIGN_START,
                              LV_FLEX_ALIGN_START, LV_FLEX_ALIGN_START);
        s_d_row[i] = row;

        lv_obj_t *bub = lv_obj_create(row);
        lv_obj_remove_style_all(bub);
        lv_obj_set_width(bub, BUB_MIN);
        lv_obj_set_height(bub, LV_SIZE_CONTENT);
        lv_obj_set_style_bg_color(bub, lv_color_hex(C_CARD), 0);
        lv_obj_set_style_bg_opa(bub, LV_OPA_COVER, 0);
        lv_obj_set_style_radius(bub, 8, 0);
        lv_obj_set_style_pad_all(bub, 7, 0);
        lv_obj_set_style_pad_row(bub, 3, 0);
        lv_obj_clear_flag(bub, LV_OBJ_FLAG_SCROLLABLE);
        lv_obj_set_flex_flow(bub, LV_FLEX_FLOW_COLUMN);
        lv_obj_set_flex_align(bub, LV_FLEX_ALIGN_START,
                              LV_FLEX_ALIGN_START, LV_FLEX_ALIGN_START);
        s_d_bub[i] = bub;

        s_d_role[i] = label_new(bub, "", &lv_font_montserrat_12, C_DIM);
        s_d_text[i] = label_new(bub, "", &lv_font_wb_14, C_TEXT);
        lv_obj_set_width(s_d_text[i], BUB_MIN - BUB_PAD);
        lv_label_set_long_mode(s_d_text[i], LV_LABEL_LONG_WRAP);
        lv_obj_add_flag(row, LV_OBJ_FLAG_HIDDEN);
    }

    lv_obj_t *hint = label_new(s_detail, "OK Talk    UP/DN Page    UP@first Back",
                               &lv_font_montserrat_12, C_DIM);
    lv_obj_align(hint, LV_ALIGN_BOTTOM_MID, 0, -10);
}

static void detail_reload(void)
{
    // 滚到底：最新的问答在最下面，开机就能看到刚发出去的那句
    lv_obj_scroll_to_y(s_d_body, LV_COORD_MAX, LV_ANIM_OFF);

    int total = wb_ble_conv_count();
    if (s_detail_idx >= 0 && s_detail_idx < total) {
        wb_conv_t c;
        if (wb_ble_conv_get((uint8_t)s_detail_idx, &c)) {
            lv_label_set_text(s_d_hdr, c.title[0] ? c.title : "(untitled)");
        }
    }

    uint16_t real_total = wb_ble_msg_total();
    int have = wb_ble_msg_count();

    for (int i = 0; i < MSG_SLOTS; i++) {
        lv_obj_t *row = s_d_row[i];
        if (i < have) {
            wb_msg_t m;
            if (wb_ble_msg_get((uint8_t)i, &m)) {
                const bool me = (m.role != WB_ROLE_AI);
                const int  bw = bubble_width(m.text);

                lv_obj_clear_flag(row, LV_OBJ_FLAG_HIDDEN);
                // 自己说的靠右，WorkBuddy 说的靠左 —— 一问一答一眼分得清
                lv_obj_set_flex_align(row,
                                      me ? LV_FLEX_ALIGN_END : LV_FLEX_ALIGN_START,
                                      LV_FLEX_ALIGN_START, LV_FLEX_ALIGN_START);
                lv_obj_set_width(s_d_bub[i], bw);
                lv_obj_set_style_bg_color(s_d_bub[i],
                                          lv_color_hex(me ? C_BUB_ME : C_BUB_AI), 0);

                lv_obj_set_width(s_d_role[i], bw - BUB_PAD);
                lv_obj_set_width(s_d_text[i], bw - BUB_PAD);
                lv_label_set_text(s_d_role[i], me ? "YOU" : "WORKBUDDY");
                lv_obj_set_style_text_color(s_d_role[i],
                                            lv_color_hex(me ? C_OK : C_ACCENT), 0);
                lv_label_set_text(s_d_text[i], m.text);
            }
        } else {
            lv_obj_add_flag(row, LV_OBJ_FLAG_HIDDEN);
        }
    }

    if (have == 0) {
        lv_obj_clear_flag(s_d_row[0], LV_OBJ_FLAG_HIDDEN);
        lv_obj_set_flex_align(s_d_row[0], LV_FLEX_ALIGN_START,
                              LV_FLEX_ALIGN_START, LV_FLEX_ALIGN_START);
        lv_obj_set_width(s_d_bub[0], BUB_MAX);
        lv_obj_set_style_bg_color(s_d_bub[0], lv_color_hex(C_BUB_AI), 0);
        lv_obj_set_width(s_d_role[0], BUB_MAX - BUB_PAD);
        lv_obj_set_width(s_d_text[0], BUB_MAX - BUB_PAD);
        lv_label_set_text(s_d_role[0], "STATUS");
        lv_obj_set_style_text_color(s_d_role[0], lv_color_hex(C_DIM), 0);
        lv_label_set_text(s_d_text[0],
                          wb_ble_is_connected() ? "No messages on this page."
                                                : "PC bridge offline.");
    }

    ESP_LOGI(TAG, "detail reload: idx=%d page=%d shown=%d total=%u",
             s_detail_idx, s_msg_page, have, (unsigned)real_total);
}

// ---------------------------------------------------------------------------
// RECORD
// ---------------------------------------------------------------------------

static void record_build(void)
{
    s_rec = screen_new();

    lv_obj_t *hdr = card_new(s_rec, 8, 6, SCR_W - 16, 34);
    s_r_dot = lv_obj_create(hdr);
    lv_obj_remove_style_all(s_r_dot);
    lv_obj_set_size(s_r_dot, 10, 10);
    lv_obj_set_pos(s_r_dot, 10, 12);
    lv_obj_set_style_radius(s_r_dot, LV_RADIUS_CIRCLE, 0);
    lv_obj_set_style_bg_opa(s_r_dot, LV_OPA_COVER, 0);
    lv_obj_set_style_bg_color(s_r_dot, lv_color_hex(C_DIM), 0);

    s_r_title = label_new(hdr, "VOICE", &lv_font_montserrat_14, C_TEXT);
    lv_obj_set_pos(s_r_title, 28, 7);

    s_r_time = label_new(hdr, "00:00", &lv_font_montserrat_14, C_ACCENT);
    lv_obj_align(s_r_time, LV_ALIGN_RIGHT_MID, -4, 0);

    // 波形区（录音态显示）
    s_r_wave = card_new(s_rec, 8, 50, SCR_W - 16, 110);
    for (int i = 0; i < BAR_COUNT; i++) {
        lv_obj_t *b = lv_obj_create(s_r_wave);
        lv_obj_remove_style_all(b);
        lv_obj_set_size(b, 8, 4);
        lv_obj_set_pos(b, 8 + i * 11, 84);
        lv_obj_set_style_radius(b, 3, 0);
        lv_obj_set_style_bg_opa(b, LV_OPA_COVER, 0);
        lv_obj_set_style_bg_color(b, lv_color_hex(C_ACCENT), 0);
        s_r_bar[i] = b;
    }

    // 转写回显卡片（确认态显示，与波形卡片同位互斥）
    s_r_heard = card_new(s_rec, 8, 50, SCR_W - 16, 124);
    lv_obj_t *ht = label_new(s_r_heard, "HEARD", &lv_font_montserrat_12, C_DIM);
    lv_obj_set_pos(ht, 8, 6);

    s_r_heard_txt = label_new(s_r_heard, "", &lv_font_wb_14, C_TEXT);
    lv_obj_set_pos(s_r_heard_txt, 8, 26);
    lv_obj_set_width(s_r_heard_txt, SCR_W - 16 - 16);
    lv_label_set_long_mode(s_r_heard_txt, LV_LABEL_LONG_WRAP);
    lv_obj_add_flag(s_r_heard, LV_OBJ_FLAG_HIDDEN);

    s_r_sub = label_new(s_rec, "Press OK to start", &lv_font_montserrat_14, C_TEXT);
    lv_obj_align(s_r_sub, LV_ALIGN_TOP_MID, 0, 180);

    s_r_hint = label_new(s_rec, "Recording, press OK to stop & send",
                         &lv_font_montserrat_12, C_DIM);
    lv_obj_set_width(s_r_hint, SCR_W - 30);
    lv_obj_set_style_text_align(s_r_hint, LV_TEXT_ALIGN_CENTER, 0);
    lv_obj_align(s_r_hint, LV_ALIGN_TOP_MID, 0, 206);

    lv_obj_t *hint = label_new(s_rec, "OK Send    UP Cancel",
                               &lv_font_montserrat_12, C_DIM);
    lv_obj_align(hint, LV_ALIGN_BOTTOM_MID, 0, -10);
}

static void record_paint_bars(void)
{
    for (int i = 0; i < BAR_COUNT; i++) {
        lv_obj_set_height(s_r_bar[i], 4);
        lv_obj_set_y(s_r_bar[i], 94);      // 与 record_tick 的基线公式一致
    }                                      // （原来写 84，首帧后整体跳 10px）
}

static void record_tick(void)
{
    if (s_rec_state != REC_RECORDING) return;

    uint8_t lv = wb_voice_level();          // 0..15
    int h = 4 + (int)lv * 6;                // 4..94
    lv_obj_set_height(s_r_bar[s_bar_next], h);
    lv_obj_set_y(s_r_bar[s_bar_next], 4 + (94 - h));
    s_bar_next = (uint8_t)((s_bar_next + 1) % BAR_COUNT);

    char tb[16];
    fmt_clock(tb, sizeof(tb), wb_voice_ms() / 1000);
    lv_label_set_text(s_r_time, tb);
}

static void record_begin(void)
{
    s_rec_state = REC_RECORDING;
    s_bar_next = 0;
    record_paint_bars();
    lv_obj_clear_flag(s_r_wave, LV_OBJ_FLAG_HIDDEN);
    lv_obj_add_flag(s_r_heard, LV_OBJ_FLAG_HIDDEN);
    s_asr_buf[0] = '\0';
    lv_obj_set_style_bg_color(s_r_dot, lv_color_hex(C_ERR), 0);
    lv_label_set_text(s_r_title, "RECORDING");
    lv_label_set_text(s_r_sub, "Speak now...");
    lv_label_set_text(s_r_hint, "Press OK to stop and send to WorkBuddy");
    lv_label_set_text(s_r_time, "00:00");

    if (wb_voice_start() != ESP_OK) {
        s_rec_state = REC_IDLE;
        lv_label_set_text(s_r_title, "MIC ERROR");
        lv_obj_set_style_bg_color(s_r_dot, lv_color_hex(C_ERR), 0);
    } else {
        ESP_LOGI(TAG, "voice start");
    }
}

static void record_finish(void)
{
    if (s_rec_state != REC_RECORDING) return;

    uint32_t ms = wb_voice_ms();
    wb_voice_stop();

    if (ms < 300) {
        s_rec_state = REC_IDLE;
        lv_label_set_text(s_r_title, "TOO SHORT");
        lv_label_set_text(s_r_sub, "Hold on, try again");
        lv_obj_set_style_bg_color(s_r_dot, lv_color_hex(C_WARN), 0);
        return;
    }

    s_rec_state = REC_TRANSCRIBING;
    lv_obj_set_style_bg_color(s_r_dot, lv_color_hex(C_WARN), 0);
    lv_label_set_text(s_r_title, "TRANSCRIBING");
    lv_label_set_text(s_r_sub, "On PC...");
    lv_label_set_text(s_r_hint, "You will see the text before it is sent");

    // 通知 PC：语音传完了，请转写**但先别发** —— mode=1 要求回显等确认
    uint8_t pl[2] = { (uint8_t)(s_detail_idx < 0 ? 0 : s_detail_idx),
                      WB_SEND_CONFIRM };
    wb_ble_notify(WB_U_SEND, pl, sizeof(pl));
    ESP_LOGI(TAG, "voice uploaded, conv=%u awaiting transcript", pl[0]);
}

// 收到 WB_D_ASR：把转写摊在屏幕上，等人点头再发
static void record_show_asr(bool ok, const char *text)
{
    if (s_page != PAGE_RECORD || !s_rec) return;

    if (!ok) {
        // 出错就停在屏幕上等人处理（s_done_at=0 表示不自动跳走），
        // 否则 2.5 秒一闪而过，字都没看清就回对话了。
        s_rec_state = REC_DONE;
        s_done_at = 0;
        lv_obj_set_style_bg_color(s_r_dot, lv_color_hex(C_ERR), 0);
        lv_label_set_text(s_r_title, "ASR FAILED");
        lv_label_set_text(s_r_sub, text[0] ? text : "No speech detected");
        lv_label_set_text(s_r_hint, "Press OK to talk again");
        return;
    }

    strncpy(s_asr_buf, text, sizeof(s_asr_buf) - 1);
    s_asr_buf[sizeof(s_asr_buf) - 1] = '\0';

    s_rec_state = REC_CONFIRM;
    lv_obj_clear_flag(s_r_heard, LV_OBJ_FLAG_HIDDEN);
    lv_obj_add_flag(s_r_wave, LV_OBJ_FLAG_HIDDEN);
    lv_label_set_text(s_r_heard_txt, s_asr_buf);
    lv_obj_set_style_bg_color(s_r_dot, lv_color_hex(C_ACCENT), 0);
    lv_label_set_text(s_r_title, "CONFIRM");
    lv_label_set_text(s_r_sub, "Send to WorkBuddy?");
    lv_label_set_text(s_r_hint, "Check the text above, then press OK");
    mark_active();
    ESP_LOGI(TAG, "transcript ready (%u bytes)", (unsigned)strlen(s_asr_buf));
}

// 确认发送：让 PC 投递它缓存的那份转写
static void record_confirm_send(void)
{
    if (s_rec_state != REC_CONFIRM) return;

    s_rec_state = REC_SENDING;
    lv_obj_set_style_bg_color(s_r_dot, lv_color_hex(C_WARN), 0);
    lv_label_set_text(s_r_title, "SENDING");
    lv_label_set_text(s_r_sub, "Delivering...");
    lv_label_set_text(s_r_hint, "Waiting for WorkBuddy");

    uint8_t idx = (uint8_t)(s_detail_idx < 0 ? 0 : s_detail_idx);
    wb_ble_notify(WB_U_CONFIRM, &idx, 1);
    ESP_LOGI(TAG, "confirm send, conv=%u", (unsigned)idx);
}

// ---------------------------------------------------------------------------
// 页面切换
// ---------------------------------------------------------------------------

static void switch_page(wb_page_t p)
{
    s_page = p;
    switch (p) {
    case PAGE_HOME:
        if (!s_home) home_build();
        lv_screen_load(s_home);
        home_refresh();
        break;

    case PAGE_LIST:
        if (!s_list) list_build();
        lv_screen_load(s_list);
        list_refresh();
        break;

    case PAGE_DETAIL:
        if (!s_detail) detail_build();
        lv_screen_load(s_detail);
        detail_reload();
        break;

    case PAGE_RECORD:
        if (!s_rec) record_build();
        lv_screen_load(s_rec);
        break;

    default:
        break;
    }
}

// ---------------------------------------------------------------------------
// 定时器
// ---------------------------------------------------------------------------

static void wave_cb(lv_timer_t *t)
{
    (void)t;
    record_tick();
}

static void tick_cb(lv_timer_t *t)
{
    (void)t;

    wb_status_t fresh;
    if (wb_ble_take_status(&fresh)) {
        s_st = fresh;
        s_have_data = true;
        s_last_frame = now_ms();          // 链路活着的证据
    }

    // 对话列表版本变化 → 列表页重建
    uint32_t cv = wb_ble_conv_ver();
    if (cv != s_conv_ver_ui) {
        s_conv_ver_ui = cv;
        if (s_page == PAGE_LIST && s_list) list_refresh();
    }

    // 消息版本变化 → 详情页重建
    uint32_t mv = wb_ble_msg_ver();
    if (mv != s_msg_ver_ui) {
        s_msg_ver_ui = mv;
        if (s_page == PAGE_DETAIL && s_detail) detail_reload();
    }

    if (s_page == PAGE_HOME && s_home) home_refresh();

    // 录音页：转写回显 / 发送结果
    if (s_page == PAGE_RECORD && s_rec) {
        // 固件侧 60s 上限到点，voice 任务自己推了 VOICE_END 并退出 ——
        // 但那样只停了录音，U_SEND 不会发出，PC 永远不会转写，屏幕会
        // 永远停在 RECORDING。这里检测到任务已停就走正常收尾流程。
        if (s_rec_state == REC_RECORDING && !wb_voice_active()) {
            record_finish();
        }
        // 转写文本先到 —— 这一帧把屏幕切进 CONFIRM 态
        bool aok;
        char asr[WB_ASR_LEN + 1];
        if (wb_ble_asr_get(&aok, asr, sizeof(asr))) {
            record_show_asr(aok, asr);
        }

        bool ok;
        char msg[40];
        if (s_rec_state == REC_SENDING && wb_ble_sent_get(&ok, msg, sizeof(msg))) {
            s_rec_state = REC_DONE;
            s_done_at = now_ms();
            lv_obj_set_style_bg_color(s_r_dot, lv_color_hex(ok ? C_OK : C_ERR), 0);
            lv_label_set_text(s_r_title, ok ? "SENT" : "FAILED");
            lv_label_set_text(s_r_sub, msg[0] ? msg : (ok ? "Delivered" : "No reply"));
            lv_label_set_text(s_r_hint, ok ? "WorkBuddy is replying" : "Check PC bridge");
        }
        if (s_rec_state == REC_DONE && s_done_at &&
            (now_ms() - s_done_at) > 2500) {
            s_rec_state = REC_IDLE;
            switch_page(PAGE_DETAIL);
        }
        // 转写与发送期间保持亮屏，别中途息屏让人以为死机
        if (s_rec_state == REC_RECORDING || s_rec_state == REC_TRANSCRIBING ||
            s_rec_state == REC_SENDING) {
            mark_active();
        }
    }

    // 息屏
    uint32_t idle = now_ms() - s_last_active;
    if (!s_dimmed && !s_off && idle > IDLE_DIM_MS) {
        s_dimmed = true;
        bsp_display_backlight(BRIGHT[2]);
    }
    // 只做一次：背光 0 是 I2C 写，每 tick 重复写纯属浪费（还可能在
    // 某些面板上引起闪一下）。重新点亮统一走 mark_active() 的 s_off 分支。
    if (!s_off && idle > IDLE_OFF_MS) {
        s_off = true;
        s_dimmed = false;
        bsp_display_backlight(0);
    }
}

// ---------------------------------------------------------------------------
// 对外
// ---------------------------------------------------------------------------

esp_err_t wb_app_init(void)
{
    ESP_LOGI(TAG, "init: build home (free=%u)", (unsigned)esp_get_free_heap_size());
    home_build();

    s_last_active = now_ms();
    bsp_display_backlight(BRIGHT[s_bright_idx]);

    s_tick = lv_timer_create(tick_cb, 500, NULL);
    s_wave = lv_timer_create(wave_cb, 80, NULL);

    esp_err_t err = wb_ble_init();
    if (err != ESP_OK) {
        ESP_LOGW(TAG, "BLE init failed: %s", esp_err_to_name(err));
    }
    ESP_LOGI(TAG, "init: done ble=%s free=%u", esp_err_to_name(err),
             (unsigned)esp_get_free_heap_size());
    return ESP_OK;
}

void wb_app_enter(void)
{
    s_page = PAGE_HOME;
    if (!s_home) home_build();
    lv_screen_load(s_home);
    home_refresh();
    mark_active();

    // 进页面就向 PC 要一次全量数据
    req_data(WB_REQ_ALL, 0);
    ESP_LOGI(TAG, "entered, free=%u", (unsigned)esp_get_free_heap_size());
}

void wb_app_exit(void)
{
    // 退出时把录音页状态机整个归零：回来时是干净的 IDLE，
    // 不会有半截的 TRANSCRIBING 卡在屏幕上。
    if (s_rec_state == REC_RECORDING) {
        wb_voice_stop();
    }
    if (s_rec_state != REC_IDLE) {
        s_rec_state = REC_IDLE;
    }
    ESP_LOGI(TAG, "exited");
}

void wb_app_key(bsp_btn_t btn, bsp_btn_ev_t ev)
{
    mark_active();                       // 唤醒优先，任何事件都算活动
    if (ev != BSP_BTN_CLICK) return;

    switch (s_page) {
    case PAGE_HOME:
        if (btn == BSP_BTN_OK) {
            s_sel = 0;
            s_top = 0;
            switch_page(PAGE_LIST);
            req_data(WB_REQ_LIST, 0);
        } else if (btn == BSP_BTN_UP) {
            s_bright_idx = (uint8_t)((s_bright_idx + 1) % 3);
            bsp_display_backlight(BRIGHT[s_bright_idx]);
            s_dimmed = false;
        } else if (btn == BSP_BTN_DOWN) {
            req_data(WB_REQ_ALL, 0);
        }
        break;

    case PAGE_LIST: {
        int total = wb_ble_conv_count();
        if (btn == BSP_BTN_DOWN) {
            if (total > 0 && s_sel < total - 1) { s_sel++; list_refresh(); }
        } else if (btn == BSP_BTN_UP) {
            if (s_sel > 0) { s_sel--; list_refresh(); }
            else switch_page(PAGE_HOME);          // 首项再 UP → 回主屏
        } else if (btn == BSP_BTN_OK) {
            if (total > 0) {
                s_detail_idx = s_sel;
                s_msg_page = 0;
                switch_page(PAGE_DETAIL);
                uint8_t pl[2] = { (uint8_t)s_detail_idx, 0 };
                wb_ble_notify(WB_U_SELECT, pl, sizeof(pl));
                req_data(WB_REQ_MSG, 0);
            }
        }
        break;
    }

    case PAGE_DETAIL:
        if (btn == BSP_BTN_OK) {
            switch_page(PAGE_RECORD);
            record_begin();
        } else if (btn == BSP_BTN_UP) {
            if (s_msg_page > 0) {
                s_msg_page--;
                req_data(WB_REQ_MSG, (uint8_t)s_msg_page);
                detail_reload();
            } else {
                switch_page(PAGE_LIST);           // 首页再 UP → 回列表
            }
        } else if (btn == BSP_BTN_DOWN) {
            if (s_msg_page < 9) {
                s_msg_page++;
                req_data(WB_REQ_MSG, (uint8_t)s_msg_page);
                detail_reload();
            }
        }
        break;

    case PAGE_RECORD:
        if (btn == BSP_BTN_OK) {
            if (s_rec_state == REC_RECORDING) {
                record_finish();
            } else if (s_rec_state == REC_CONFIRM) {
                record_confirm_send();
            } else if (s_rec_state == REC_IDLE || s_rec_state == REC_DONE) {
                record_begin();
            }
            // TRANSCRIBING / SENDING 期间按 OK 不响应，防重复提交
        } else if (btn == BSP_BTN_UP) {
            if (s_rec_state == REC_RECORDING) {
                wb_voice_stop();
                s_rec_state = REC_IDLE;
                lv_obj_set_style_bg_color(s_r_dot, lv_color_hex(C_WARN), 0);
                lv_label_set_text(s_r_title, "CANCELLED");
                lv_label_set_text(s_r_sub, "Press OK to talk again");
                lv_label_set_text(s_r_hint, "Nothing was sent");
                lv_obj_clear_flag(s_r_wave, LV_OBJ_FLAG_HIDDEN);
                lv_obj_add_flag(s_r_heard, LV_OBJ_FLAG_HIDDEN);
                record_paint_bars();
            } else {
                // 确认态按 UP = 这句不发了，退回对话
                s_rec_state = REC_IDLE;
                switch_page(PAGE_DETAIL);
            }
        }
        break;

    default:
        break;
    }
}
