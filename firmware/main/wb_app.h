// main/wb_app.h —— WorkBuddy 随身终端：应用层对外接口。
//
// 与 demo 菜单并存：main.c 在菜单里加一个入口 "WorkBuddy"，
// 进入后整屏交给本模块，长按 OK 返回菜单。
#pragma once

#include "bsp_button.h"
#include "esp_err.h"
#include "lvgl.h"

// 初始化：建屏、建定时器、准备 BLE 外设。必须在 LVGL 已初始化后调用。
esp_err_t wb_app_init(void);

// 进入/退出（由 main.c 的菜单调度）
void wb_app_enter(void);
void wb_app_exit(void);

// 按键分发（在 LVGL 锁内执行）
void wb_app_key(bsp_btn_t btn, bsp_btn_ev_t ev);
