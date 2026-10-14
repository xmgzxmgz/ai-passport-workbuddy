// main/demo_workbuddy.c —— 菜单适配层：把 wb_app 挂进 demo_entry_t 机制。
//
// 拆成独立文件是为了不动 wb_app.c 的职责边界：
//   wb_app.* 只关心 UI 与数据，不认识 demo 菜单；
//   本文件只做 enter/exit/key/start/stop 的胶水。
#include "demo.h"
#include "demo_radio.h"
#include "wb_app.h"
#include "wb_ble.h"

#include "bsp_display.h"

#include "esp_log.h"

static const char *TAG = "demo_wb";

static bool s_inited;

// 开机常驻：NVS 就绪 + BLE 开始广播。
// 放在 app_main 里调，让工牌一上电就能被 PC 侧发现，不必先进页面。
esp_err_t demo_workbuddy_boot(void)
{
    esp_err_t e = demo_radio_nvs_prepare();
    if (e != ESP_OK) {
        ESP_LOGW(TAG, "nvs prepare failed: %s", esp_err_to_name(e));
    }
    e = wb_ble_init();
    if (e != ESP_OK) {
        ESP_LOGW(TAG, "resident BLE init failed: %s", esp_err_to_name(e));
    } else {
        ESP_LOGI(TAG, "resident BLE ready");
    }
    return e;
}

void demo_workbuddy_enter(void)
{
    if (!s_inited) {
        // BLE 通常已由 demo_workbuddy_boot 拉起；wb_ble_init 幂等，重复调用安全。
        esp_err_t e = wb_ble_init();
        if (e != ESP_OK) {
            ESP_LOGW(TAG, "BLE init failed: %s", esp_err_to_name(e));
        }
        if (wb_app_init() != ESP_OK) {
            ESP_LOGE(TAG, "wb_app_init failed");
        }
        s_inited = true;
    }
    wb_app_enter();
}

void demo_workbuddy_exit(void)
{
    wb_app_exit();
}

void demo_workbuddy_key(bsp_btn_t btn, bsp_btn_ev_t ev)
{
    // 必须自己持 LVGL 锁：本回调由 demo_input 任务直接调用（main.c 的
    // DEMO_NAV_ACTION_FORWARD 分支不加锁），而 wb_app_key 会建/改 LVGL 对象。
    // 漏锁的后果不是"偶尔花屏"，而是 LVGL 在 lv_inv_area 里断言
    //   LV_ASSERT_MSG(!disp->rendering_in_progress, ...)
    // 其默认处理是 while(1) —— 任务永久挂起，表现为 task_wdt 反复报
    // "CPU 0: demo_input"，页面闪一下就没了。菜单里其他 7 个 demo 都自己加了锁。
    if (!bsp_lvgl_lock(250)) return;
    wb_app_key(btn, ev);
    bsp_lvgl_unlock();
}

esp_err_t demo_workbuddy_start(void)
{
    // BLE 与 NVS 都已在 demo_workbuddy_enter 里就绪，这里无需重复。
    return ESP_OK;
}

esp_err_t demo_workbuddy_stop(void)
{
    // 页面退出时不拆 BLE：中转服务需要持续连接。
    return ESP_OK;
}
