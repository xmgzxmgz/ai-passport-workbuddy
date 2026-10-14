// main/wb_ble.c —— AI Passport 双向 BLE 外设。
//
//   FF01 Write  ← PC 下发（状态 / 对话列表 / 消息 / 额度 / 语音授权）
//   FF02 Notify → 工牌上报（选择 / 刷新请求 / 语音帧 / 发送请求）
//
// 数据全部落在静态缓冲里（不占堆），UI 侧通过带锁的 getter 取用。
#include "wb_ble.h"

#include "esp_log.h"
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"
#include "freertos/task.h"
#include "host/ble_att.h"
#include "host/ble_gap.h"
#include "host/ble_hs.h"
#include "host/util/util.h"
#include "nimble/nimble_port.h"
#include "services/gap/ble_svc_gap.h"
#include "services/gatt/ble_svc_gatt.h"

#include <string.h>

static const char *TAG = "wb_ble";

static const char *DEVICE_NAME = "AI-Passport";

static const ble_uuid16_t SVC_UUID = BLE_UUID16_INIT(0xFF00);
static const ble_uuid16_t CHR_DOWN = BLE_UUID16_INIT(0xFF01);
static const ble_uuid16_t CHR_UP   = BLE_UUID16_INIT(0xFF02);

// 单帧上限。必须 >= mtu_payload() 上限（MTU 247 时为 244），否则
// ble_hs_mbuf_to_flat 会把超过 192B 的下行写直接判成 EMSGSIZE 丢掉；
// 而 voice_push 的块长也按这个值收敛，写不下就会砸栈。
#define WB_FRAME_MAX   264

// ---------------------------------------------------------------------------
// 下行数据缓存（BLE 任务写，LVGL 任务读，portMUX 保护）
// ---------------------------------------------------------------------------

static wb_status_t s_status;
static volatile bool s_status_new;

static wb_conv_t  s_convs[WB_MAX_CONV];
static uint8_t    s_conv_count;
static uint32_t   s_conv_ver;

static wb_msg_t   s_msgs[WB_MAX_MSG];
static uint8_t    s_msg_count;
static uint16_t   s_msg_total;
static uint32_t   s_msg_ver;

static char       s_usage[52];
static char       s_info[28];
static char       s_sent_text[40];
static bool       s_sent_ok;
static volatile bool s_sent_new;
static char       s_asr_text[WB_ASR_LEN + 1];
static bool       s_asr_ok;
static volatile bool s_asr_new;
static volatile bool s_voice_granted;

static portMUX_TYPE s_lock = portMUX_INITIALIZER_UNLOCKED;

// ---------------------------------------------------------------------------
// 连接状态
// ---------------------------------------------------------------------------

static uint8_t  s_addr_type;
static uint16_t s_conn_handle = BLE_HS_CONN_HANDLE_NONE;
static volatile bool s_connected;
static volatile bool s_started;
static uint16_t s_mtu = 23;
static uint16_t s_chr_up_handle;         // FF02 value handle
static SemaphoreHandle_t s_host_stopped;
static TaskHandle_t s_host_task;
static bool s_host_done;

static uint16_t voice_seq;

// ---------------------------------------------------------------------------
// 下行帧解析
// ---------------------------------------------------------------------------

static void clear_convs_locked(void)
{
    s_conv_count = 0;
    s_conv_ver++;
}

static void clear_msgs_locked(void)
{
    s_msg_count = 0;
    s_msg_total = 0;
    s_msg_ver++;
}

static void handle_downlink(const uint8_t *p, uint16_t len)
{
    if (len < 5 || p[0] != WB_D_MAGIC) return;

    const uint8_t cmd   = p[1];
    const uint8_t seq   = p[2];
    const uint8_t total = p[3];
    const uint8_t *pl   = p + 4;
    const uint16_t plen = (uint16_t)(len - 4);

    switch (cmd) {
    case WB_D_STATUS: {
        if (plen < 5) return;
        wb_status_t st;
        memset(&st, 0, sizeof(st));
        st.state    = pl[0];
        st.online   = (pl[1] & 0x02) != 0;
        st.uptime_s = (uint16_t)(pl[2] | (pl[3] << 8));
        uint8_t nlen = pl[4];
        if (nlen > WB_TITLE_LEN) nlen = WB_TITLE_LEN;
        if ((uint16_t)(5 + nlen) > plen) nlen = (uint8_t)(plen - 5);
        memcpy(st.title, &pl[5], nlen);
        st.title[nlen] = '\0';

        portENTER_CRITICAL(&s_lock);
        s_status = st;
        s_status_new = true;
        portEXIT_CRITICAL(&s_lock);
        break;
    }

    case WB_D_CONV: {
        if (seq >= WB_MAX_CONV || plen < 2) return;
        wb_conv_t c;
        memset(&c, 0, sizeof(c));
        c.status = pl[0];
        uint8_t tlen = pl[1];
        if (tlen > WB_TITLE_LEN) tlen = WB_TITLE_LEN;
        if ((uint16_t)(2 + tlen) > plen) tlen = (uint8_t)(plen - 2);
        memcpy(c.title, &pl[2], tlen);
        c.title[tlen] = '\0';

        portENTER_CRITICAL(&s_lock);
        s_convs[seq] = c;
        if ((uint8_t)(seq + 1) > s_conv_count) s_conv_count = (uint8_t)(seq + 1);
        if (total && total < s_conv_count) s_conv_count = total;
        s_conv_ver++;
        portEXIT_CRITICAL(&s_lock);
        break;
    }

    case WB_D_MSG: {
        if (seq >= WB_MAX_MSG || plen < 2) return;
        wb_msg_t m;
        memset(&m, 0, sizeof(m));
        m.role = pl[0];
        uint8_t tlen = pl[1];
        if (tlen > WB_TEXT_LEN) tlen = WB_TEXT_LEN;
        if ((uint16_t)(2 + tlen) > plen) tlen = (uint8_t)(plen - 2);
        memcpy(m.text, &pl[2], tlen);
        m.text[tlen] = '\0';

        portENTER_CRITICAL(&s_lock);
        s_msgs[seq] = m;
        if ((uint8_t)(seq + 1) > s_msg_count) s_msg_count = (uint8_t)(seq + 1);
        s_msg_total = total;
        s_msg_ver++;
        portEXIT_CRITICAL(&s_lock);
        break;
    }

    case WB_D_USAGE: {
        uint16_t n = plen < sizeof(s_usage) - 1 ? plen : sizeof(s_usage) - 1;
        portENTER_CRITICAL(&s_lock);
        memcpy(s_usage, pl, n);
        s_usage[n] = '\0';
        portEXIT_CRITICAL(&s_lock);
        break;
    }

    case WB_D_INFO: {
        uint16_t n = plen < sizeof(s_info) - 1 ? plen : sizeof(s_info) - 1;
        portENTER_CRITICAL(&s_lock);
        memcpy(s_info, pl, n);
        s_info[n] = '\0';
        portEXIT_CRITICAL(&s_lock);
        break;
    }

    case WB_D_VOICE: {
        if (plen < 1) return;
        s_voice_granted = (pl[0] == WB_VOICE_READY);
        break;
    }

    case WB_D_SENT: {
        if (plen < 1) return;
        uint16_t n = (uint16_t)(plen - 1);
        if (n > sizeof(s_sent_text) - 1) n = sizeof(s_sent_text) - 1;
        portENTER_CRITICAL(&s_lock);
        s_sent_ok = (pl[0] == 0);
        memcpy(s_sent_text, &pl[1], n);
        s_sent_text[n] = '\0';
        s_sent_new = true;
        portEXIT_CRITICAL(&s_lock);
        break;
    }

    case WB_D_ASR: {
        if (plen < 1) return;
        uint16_t n = (uint16_t)(plen - 1);
        if (n > WB_ASR_LEN) n = WB_ASR_LEN;
        portENTER_CRITICAL(&s_lock);
        s_asr_ok = (pl[0] == 0);
        memcpy(s_asr_text, &pl[1], n);
        s_asr_text[n] = '\0';
        s_asr_new = true;
        portEXIT_CRITICAL(&s_lock);
        break;
    }

    case WB_D_CLEAR: {
        uint8_t what = plen ? pl[0] : 0;
        portENTER_CRITICAL(&s_lock);
        if (what == 0 || what == 1) clear_convs_locked();
        if (what == 0 || what == 2) clear_msgs_locked();
        portEXIT_CRITICAL(&s_lock);
        break;
    }

    default:
        ESP_LOGW(TAG, "unknown downlink cmd 0x%02X", cmd);
        break;
    }
}

// ---------------------------------------------------------------------------
// 上行发送
// ---------------------------------------------------------------------------

static uint16_t mtu_payload(void)
{
    uint16_t m = s_mtu;
    if (m < 23) m = 23;
    return (uint16_t)(m - 3);
}

uint16_t wb_ble_mtu(void)
{
    return s_mtu;
}

bool wb_ble_is_connected(void)
{
    return s_connected;
}

esp_err_t wb_ble_notify(uint8_t cmd, const void *payload, uint16_t plen)
{
    if (!s_connected || s_conn_handle == BLE_HS_CONN_HANDLE_NONE) {
        return ESP_ERR_INVALID_STATE;
    }

    uint16_t maxp = mtu_payload();
    if ((uint16_t)(plen + 2) > maxp) {
        plen = (uint16_t)(maxp - 2);        // 截断：上行帧只带头 + cmd
    }

    uint8_t buf[WB_FRAME_MAX];
    if ((uint16_t)(plen + 2) > sizeof(buf)) return ESP_ERR_INVALID_SIZE;
    buf[0] = WB_U_MAGIC;
    buf[1] = cmd;
    if (plen && payload) memcpy(&buf[2], payload, plen);

    struct os_mbuf *om = ble_hs_mbuf_from_flat(buf, (uint16_t)(plen + 2));
    if (!om) return ESP_ERR_NO_MEM;

    int rc = ble_gatts_notify_custom(s_conn_handle, s_chr_up_handle, om);
    if (rc != 0) return ESP_FAIL;
    return ESP_OK;
}

esp_err_t wb_ble_voice_push(const void *pcm, uint16_t len)
{
    if (!s_connected || !pcm || !len) return ESP_ERR_INVALID_STATE;

    const uint8_t *src = (const uint8_t *)pcm;
    uint16_t maxp = mtu_payload();
    // 帧头 4 字节（magic / cmd / seq16），其余留给 PCM。
    // 上限要同时受 MTU 和本地栈缓冲夹住 —— 少了后一项，MTU 247 下
    // room 会算成 238，memcpy 直接冲出 buf[WB_FRAME_MAX]，返回地址被写成
    // 0xfffe，表现就是「一按录音就重启」。
    uint16_t room = (maxp > 6) ? (uint16_t)(maxp - 6) : 16;
    if (room > (uint16_t)(WB_FRAME_MAX - 4)) room = (uint16_t)(WB_FRAME_MAX - 4);
    room &= (uint16_t)~1u;                  // 16bit PCM，块长取偶

    while (len) {
        uint16_t n = len < room ? len : room;
        uint8_t buf[WB_FRAME_MAX];
        buf[0] = WB_U_MAGIC;
        buf[1] = WB_U_VOICE_DATA;
        buf[2] = (uint8_t)(voice_seq & 0xFF);
        buf[3] = (uint8_t)(voice_seq >> 8);
        memcpy(&buf[4], src, n);

        // msys 的 mbuf 池只有十几个块。链路排不空时 ble_hs_mbuf_from_flat
        // 直接返回 NULL —— 原来这里一看 NULL 就整片丢弃，实测语音丢了一半。
        // 现在短等一下再试：把「丢数据」换成「轻微让出 CPU」，池排空就继续。
        struct os_mbuf *om = NULL;
        for (int t = 0; t < 8 && !om; t++) {
            om = ble_hs_mbuf_from_flat(buf, (uint16_t)(n + 4));
            if (!om) vTaskDelay(pdMS_TO_TICKS(4));
        }
        if (!om) return ESP_ERR_NO_MEM;
        if (ble_gatts_notify_custom(s_conn_handle, s_chr_up_handle, om) != 0) {
            return ESP_FAIL;
        }

        voice_seq++;
        src += n;
        len = (uint16_t)(len - n);
    }
    return ESP_OK;
}

// ---------------------------------------------------------------------------
// getter（LVGL 侧）
// ---------------------------------------------------------------------------

bool wb_ble_take_status(wb_status_t *out)
{
    bool fresh;
    portENTER_CRITICAL(&s_lock);
    fresh = s_status_new;
    if (fresh) {
        *out = s_status;
        s_status_new = false;
    }
    portEXIT_CRITICAL(&s_lock);
    return fresh;
}

uint32_t wb_ble_conv_ver(void)
{
    return s_conv_ver;
}

uint8_t wb_ble_conv_count(void)
{
    return s_conv_count;
}

bool wb_ble_conv_get(uint8_t i, wb_conv_t *out)
{
    if (i >= s_conv_count || i >= WB_MAX_CONV) return false;
    portENTER_CRITICAL(&s_lock);
    *out = s_convs[i];
    portEXIT_CRITICAL(&s_lock);
    return true;
}

uint32_t wb_ble_msg_ver(void)
{
    return s_msg_ver;
}

uint8_t wb_ble_msg_count(void)
{
    return s_msg_count;
}

uint16_t wb_ble_msg_total(void)
{
    return s_msg_total;
}

bool wb_ble_msg_get(uint8_t i, wb_msg_t *out)
{
    if (i >= s_msg_count || i >= WB_MAX_MSG) return false;
    portENTER_CRITICAL(&s_lock);
    *out = s_msgs[i];
    portEXIT_CRITICAL(&s_lock);
    return true;
}

bool wb_ble_usage_get(char *out, size_t n)
{
    bool has;
    portENTER_CRITICAL(&s_lock);
    has = s_usage[0] != '\0';
    if (has) {
        strncpy(out, s_usage, n - 1);
        out[n - 1] = '\0';
    }
    portEXIT_CRITICAL(&s_lock);
    return has;
}

bool wb_ble_info_get(char *out, size_t n)
{
    bool has;
    portENTER_CRITICAL(&s_lock);
    has = s_info[0] != '\0';
    if (has) {
        strncpy(out, s_info, n - 1);
        out[n - 1] = '\0';
    }
    portEXIT_CRITICAL(&s_lock);
    return has;
}

bool wb_ble_sent_get(bool *ok, char *out, size_t n)
{
    bool fresh;
    portENTER_CRITICAL(&s_lock);
    fresh = s_sent_new;
    if (fresh) {
        *ok = s_sent_ok;
        strncpy(out, s_sent_text, n - 1);
        out[n - 1] = '\0';
        s_sent_new = false;
    }
    portEXIT_CRITICAL(&s_lock);
    return fresh;
}

bool wb_ble_asr_get(bool *ok, char *out, size_t n)
{
    bool fresh;
    portENTER_CRITICAL(&s_lock);
    fresh = s_asr_new;
    if (fresh) {
        *ok = s_asr_ok;
        strncpy(out, s_asr_text, n - 1);
        out[n - 1] = '\0';
        s_asr_new = false;
    }
    portEXIT_CRITICAL(&s_lock);
    return fresh;
}

bool wb_ble_voice_granted(void)
{
    return s_voice_granted;
}

// ---------------------------------------------------------------------------
// GATT
// ---------------------------------------------------------------------------

static int chr_down_access(uint16_t conn_handle, uint16_t attr_handle,
                           struct ble_gatt_access_ctxt *ctxt, void *arg)
{
    (void)conn_handle; (void)attr_handle; (void)arg;

    if (ctxt->op != BLE_GATT_ACCESS_OP_WRITE_CHR) {
        return BLE_ATT_ERR_UNLIKELY;
    }

    uint8_t buf[WB_FRAME_MAX];
    uint16_t len = 0;
    int rc = ble_hs_mbuf_to_flat(ctxt->om, buf, sizeof(buf), &len);
    if (rc != 0) {
        ESP_LOGW(TAG, "mbuf flatten failed: %d", rc);
        return BLE_ATT_ERR_UNLIKELY;
    }

    handle_downlink(buf, len);
    return 0;
}

static int chr_up_access(uint16_t conn_handle, uint16_t attr_handle,
                         struct ble_gatt_access_ctxt *ctxt, void *arg)
{
    (void)conn_handle; (void)attr_handle; (void)arg; (void)ctxt;
    return 0;
}

static const struct ble_gatt_svc_def GATT_SVCS[] = {
    {
        .type = BLE_GATT_SVC_TYPE_PRIMARY,
        .uuid = &SVC_UUID.u,
        .characteristics = (struct ble_gatt_chr_def[]){
            {
                .uuid = &CHR_DOWN.u,
                .access_cb = chr_down_access,
                .flags = BLE_GATT_CHR_F_WRITE | BLE_GATT_CHR_F_WRITE_NO_RSP,
            },
            {
                .uuid = &CHR_UP.u,
                .access_cb = chr_up_access,
                .flags = BLE_GATT_CHR_F_NOTIFY,
            },
            { 0 },
        },
    },
    { 0 },
};

// ---------------------------------------------------------------------------
// 广播 / 连接管理
// ---------------------------------------------------------------------------

static int advertise(void);

static int gap_event(struct ble_gap_event *event, void *arg)
{
    (void)arg;
    switch (event->type) {
    case BLE_GAP_EVENT_CONNECT:
        if (event->connect.status == 0) {
            s_conn_handle = event->connect.conn_handle;
            s_connected = true;
            ESP_LOGI(TAG, "central connected, handle=%d", s_conn_handle);
        } else {
            ESP_LOGW(TAG, "connect failed: %d", event->connect.status);
            advertise();
        }
        return 0;

    case BLE_GAP_EVENT_DISCONNECT:
        ESP_LOGI(TAG, "disconnected, reason=%d", event->disconnect.reason);
        s_connected = false;
        s_conn_handle = BLE_HS_CONN_HANDLE_NONE;
        s_mtu = 23;
        s_voice_granted = false;
        advertise();                 // 掉线立刻重新可被扫描
        return 0;

    case BLE_GAP_EVENT_MTU:
        s_mtu = event->mtu.value;
        ESP_LOGI(TAG, "mtu negotiated = %u", (unsigned)s_mtu);
        return 0;

    case BLE_GAP_EVENT_ADV_COMPLETE:
        if (s_started) advertise();
        return 0;

    default:
        return 0;
    }
}

static int advertise(void)
{
    struct ble_hs_adv_fields fields = { 0 };
    fields.flags = BLE_HS_ADV_F_DISC_GEN | BLE_HS_ADV_F_BREDR_UNSUP;
    fields.name = (const uint8_t *)DEVICE_NAME;
    fields.name_len = (uint8_t)strlen(DEVICE_NAME);
    fields.name_is_complete = 1;

    int rc = ble_gap_adv_set_fields(&fields);
    if (rc != 0) return rc;

    struct ble_gap_adv_params params = { 0 };
    params.conn_mode = BLE_GAP_CONN_MODE_UND;    // 可连接
    params.disc_mode = BLE_GAP_DISC_MODE_GEN;

    return ble_gap_adv_start(s_addr_type, NULL, BLE_HS_FOREVER, &params,
                             gap_event, NULL);
}

static void on_reset(int reason)
{
    ESP_LOGE(TAG, "host reset: %d", reason);
}

static void on_sync(void)
{
    int rc = ble_hs_util_ensure_addr(0);
    if (rc == 0) rc = ble_hs_id_infer_auto(0, &s_addr_type);

    // FF02 的 value handle 要等 host 启动、GATT 表落位之后才存在。
    // 在 wb_ble_init() 里查会返回 BLE_HS_ENOENT(5)，必须挪到这里。
    if (rc == 0) {
        rc = ble_gatts_find_chr(&SVC_UUID.u, &CHR_UP.u, NULL, &s_chr_up_handle);
        if (rc != 0) {
            ESP_LOGE(TAG, "find_chr FF02: %d", rc);
        } else {
            ESP_LOGI(TAG, "FF02 notify handle = %u", (unsigned)s_chr_up_handle);
        }
    }

    if (rc == 0 && s_started) rc = advertise();
    if (rc != 0) ESP_LOGE(TAG, "sync failed: %d", rc);
}

static void host_task(void *arg)
{
    (void)arg;
    nimble_port_run();
    xSemaphoreGive(s_host_stopped);
    for (;;) vTaskSuspend(NULL);
}

// ---------------------------------------------------------------------------
// 对外
// ---------------------------------------------------------------------------

esp_err_t wb_ble_init(void)
{
    if (s_started) return ESP_OK;

    esp_err_t err = nimble_port_init();
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "nimble_port_init: %s", esp_err_to_name(err));
        return err;
    }

    // 让 PC 侧一次能带走整条消息（120B 正文），避免拆包重组
    ble_att_set_preferred_mtu(247);

    s_host_stopped = xSemaphoreCreateBinary();
    if (!s_host_stopped) {
        nimble_port_deinit();
        return ESP_ERR_NO_MEM;
    }

    ble_svc_gap_init();
    ble_svc_gatt_init();

    int rc = ble_svc_gap_device_name_set(DEVICE_NAME);
    if (rc != 0) {
        ESP_LOGE(TAG, "device_name_set: %d", rc);
        goto fail;
    }

    rc = ble_gatts_count_cfg(GATT_SVCS);
    if (rc != 0) {
        ESP_LOGE(TAG, "count_cfg: %d", rc);
        goto fail;
    }
    rc = ble_gatts_add_svcs(GATT_SVCS);
    if (rc != 0) {
        ESP_LOGE(TAG, "add_svcs: %d", rc);
        goto fail;
    }

    ble_hs_cfg.reset_cb = on_reset;
    ble_hs_cfg.sync_cb = on_sync;
    s_started = true;

    if (xTaskCreatePinnedToCore(host_task, "wb_nimble", 4096, NULL,
                                configMAX_PRIORITIES - 4, &s_host_task,
                                tskNO_AFFINITY) != pdPASS) {
        s_started = false;
        goto fail;
    }

    ESP_LOGI(TAG, "BLE peripheral up as \"%s\"", DEVICE_NAME);
    return ESP_OK;

fail:
    s_started = false;
    if (s_host_stopped) {
        vSemaphoreDelete(s_host_stopped);
        s_host_stopped = NULL;
    }
    nimble_port_deinit();
    return ESP_FAIL;
}

esp_err_t wb_ble_stop(void)
{
    s_started = false;
    if (!s_host_task) return ESP_OK;

    (void)ble_gap_adv_stop();
    if (s_conn_handle != BLE_HS_CONN_HANDLE_NONE) {
        ble_gap_terminate(s_conn_handle, BLE_ERR_REM_USER_CONN_TERM);
    }

    int rc = nimble_port_stop();
    if (rc != 0) ESP_LOGW(TAG, "nimble_port_stop: %d", rc);

    if (!s_host_done && s_host_stopped) {
        if (xSemaphoreTake(s_host_stopped, pdMS_TO_TICKS(2000)) == pdTRUE) {
            s_host_done = true;
        } else {
            ESP_LOGW(TAG, "host stop timeout");
        }
    }

    vTaskDelete(s_host_task);
    s_host_task = NULL;
    nimble_port_deinit();

    if (s_host_stopped) {
        vSemaphoreDelete(s_host_stopped);
        s_host_stopped = NULL;
    }
    s_host_done = false;
    s_connected = false;
    s_conn_handle = BLE_HS_CONN_HANDLE_NONE;
    return ESP_OK;
}
