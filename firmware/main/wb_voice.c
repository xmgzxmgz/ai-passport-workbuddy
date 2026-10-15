// main/wb_voice.c —— 麦克风采集任务：I2S 读 20ms，攒够 40ms 编成
// 一包 IMA ADPCM 再推 BLE。
//
// 为什么不是边读边推原始 PCM：BLE 在 Windows 侧实测上限约 10KB/s
// （≈43 包/s），8kHz/16bit 单声道 PCM 需要 16KB/s，链路先天不够，
// 结果就是设备照送、PC 收一半 —— 转写全是噪声。
// ADPCM 4bit/采样后 8kHz 只要 4KB/s，一包 40ms 即 166 字节、25 包/s。
#include "wb_voice.h"

#include "bsp_audio.h"
#include "wb_adpcm.h"
#include "wb_ble.h"
#include "wb_proto.h"

#include "esp_log.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"

#include <string.h>

static const char *TAG = "wb_voice";

#define BUF_SAMPLES  160                        // 每次 I2S 读 20ms @8kHz
#define BLOCKS       2                          // 攒 2 块 = 40ms 编一包
#define PACK_SAMPLES (BUF_SAMPLES * BLOCKS)     // 320 采样
#define ADPCM_LEN    (PACK_SAMPLES / 2)         // 160 字节
#define PKT_LEN      (2 + 2 + ADPCM_LEN)        // seq16 + seed16 + nibbles
#define TASK_STACK   6144                       // 含 codec 建链 + 264B 推送栈帧
#define VOICE_MAX_MS (60u * 1000u)              // 固件侧录音上限：到点自动 END

static TaskHandle_t      s_task;
static volatile bool     s_run;
static volatile bool     s_active;
static volatile uint32_t s_samples;
static volatile uint8_t  s_level;
static volatile uint32_t s_drops;

static void voice_task(void *arg)
{
    (void)arg;

    if (bsp_audio_init() != ESP_OK) {
        ESP_LOGE(TAG, "audio init failed");
        goto out;
    }
    // 每次录完都会 bsp_audio_sleep() 把 codec 睡下，而休眠态下 set_format 直接
    // 返回 ESP_ERR_INVALID_STATE —— 不唤醒就永远只有第一次能录。未睡时是空操作。
    if (bsp_audio_wake() != ESP_OK) {
        ESP_LOGE(TAG, "audio wake failed");
        goto out;
    }
    if (bsp_audio_set_format(WB_VOICE_RATE, 16, 1) != ESP_OK) {
        ESP_LOGE(TAG, "set_format failed");
        goto out;
    }

    // 告知 PC：录音开始 + 采样率 + 负载编码
    {
        uint8_t hdr[4] = { WB_VOICE_RATE & 0xFF, (WB_VOICE_RATE >> 8) & 0xFF,
                           WB_VOICE_CODEC_ADPCM, 0 };
        wb_ble_notify(WB_U_VOICE_START, hdr, sizeof(hdr));
    }

    int16_t    blk[BUF_SAMPLES];
    int16_t    acc[PACK_SAMPLES];
    uint8_t    pkt[PKT_LEN];
    wb_adpcm_t st;

    uint32_t acc_n = 0;
    uint16_t seq = 0;
    int16_t  seed = 0;                  // 上一包最后一个原始采样值

    while (s_run) {
        if (bsp_audio_read(blk, sizeof(blk)) != ESP_OK) {
            ESP_LOGW(TAG, "read failed, abort");
            break;
        }

        int32_t peak = 0;
        for (int i = 0; i < BUF_SAMPLES; i++) {
            int32_t v = blk[i];
            if (v < 0) v = -v;
            if (v > peak) peak = v;
        }
        uint32_t lv = (uint32_t)peak >> 9;     // 32767>>9 = 63
        s_level = lv > 15 ? 15 : (uint8_t)lv;
        s_samples += BUF_SAMPLES;

        // 固件侧时长上限：之前只靠 PC 侧静默截断，用户说 90 秒屏幕照常
        // 计时，PC 只收前 60 秒，转写缺尾巴还以为是 Whisper 不准。
        // 到点直接跳出循环，走下面的正常 VOICE_END 收尾。
        if (s_samples >= (uint32_t)(VOICE_MAX_MS * WB_VOICE_RATE / 1000u)) {
            ESP_LOGI(TAG, "hit %us cap, stopping", (unsigned)(VOICE_MAX_MS / 1000u));
            break;
        }

        memcpy(&acc[acc_n], blk, sizeof(blk));
        acc_n += BUF_SAMPLES;
        if (acc_n < PACK_SAMPLES) continue;
        acc_n = 0;

        // 每包用上一包的尾采样做 seed 重置预测器：收发两端对齐，
        // 且丢包不会连锁污染后面的包。
        wb_adpcm_reset(&st, seed);
        wb_adpcm_encode(&st, acc, PACK_SAMPLES, &pkt[4]);
        pkt[0] = (uint8_t)(seq & 0xFF);
        pkt[1] = (uint8_t)(seq >> 8);
        pkt[2] = (uint8_t)(seed & 0xFF);
        pkt[3] = (uint8_t)((uint16_t)seed >> 8);
        seed = acc[PACK_SAMPLES - 1];

        if (wb_ble_is_connected() && wb_ble_voice_push(pkt, sizeof(pkt)) == ESP_OK) {
            seq++;
        } else {
            s_drops++;
        }
    }

    {
        uint8_t tail[6];
        uint32_t smp = s_samples;
        tail[0] = (uint8_t)(seq & 0xFF);
        tail[1] = (uint8_t)(seq >> 8);
        tail[2] = (uint8_t)(smp & 0xFF);
        tail[3] = (uint8_t)((smp >> 8) & 0xFF);
        tail[4] = (uint8_t)((smp >> 16) & 0xFF);
        tail[5] = (uint8_t)((smp >> 24) & 0xFF);
        if (wb_ble_is_connected()) wb_ble_notify(WB_U_VOICE_END, tail, sizeof(tail));
        ESP_LOGI(TAG, "done: %u packets, %u samples, %u dropped, %u bytes",
                 (unsigned)seq, (unsigned)smp, (unsigned)s_drops,
                 (unsigned)(seq * ADPCM_LEN));
    }

    (void)bsp_audio_sleep();                   // 关掉 codec，省电

out:
    s_level = 0;
    s_active = false;
    s_task = NULL;
    vTaskDelete(NULL);
}

esp_err_t wb_voice_start(void)
{
    // 上一次录音的任务还没退干净（正常已被 stop 等待，这里只是兜底）。
    // 原来此处直接 return ESP_OK 假装成功，调用方以为在录，实际什么都没发生。
    if (s_task) {
        wb_voice_stop();                       // 内部会等最多 500ms
        if (s_task) return ESP_ERR_INVALID_STATE;
    }

    s_run = true;
    s_active = true;
    s_samples = 0;
    s_level = 0;
    s_drops = 0;

    if (xTaskCreatePinnedToCore(voice_task, "wb_voice", TASK_STACK, NULL,
                                configMAX_PRIORITIES - 5, &s_task,
                                tskNO_AFFINITY) != pdPASS) {
        s_run = false;
        s_active = false;
        return ESP_ERR_NO_MEM;
    }
    return ESP_OK;
}

void wb_voice_stop(void)
{
    s_run = false;
    // 等旧任务真正退出再返回。之前的竞态：UP 取消后旧任务还阻塞在
    // bsp_audio_read()，立刻按 OK 重录时 s_task 非空 -> start 直接返回
    // "已在录"，但实际没起新任务 —— 屏幕进 RECORDING 却无波形无计时，
    // 最后判 TOO SHORT。任务循环 20ms 一轮，正常 2~3 轮内退出。
    int wait_ms = 0;
    while (s_task != NULL && wait_ms < 500) {
        vTaskDelay(pdMS_TO_TICKS(10));
        wait_ms += 10;
    }
}

bool wb_voice_active(void)
{
    return s_active;
}

uint32_t wb_voice_ms(void)
{
    return (uint32_t)((uint64_t)s_samples * 1000 / WB_VOICE_RATE);
}

uint8_t wb_voice_level(void)
{
    return s_level;
}

uint32_t wb_voice_samples(void)
{
    return s_samples;
}
