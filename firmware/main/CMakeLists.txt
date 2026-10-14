idf_component_register(
    SRCS "main.c"
         "demo_navigation.c"
         "ui_pixel.c"
         "ui_pixel_math.c"
         "demo_display.c"
         "demo_button.c"
         "demo_audio.c"
         "demo_battery.c"
         "demo_radio.c"
         "demo_wifi.c"
         "demo_ble.c"
         "demo_low_power.c"
         "wb_app.c"
         "wb_ble.c"
         "wb_voice.c"
         "wb_adpcm.c"
         "demo_workbuddy.c"
         # 中文字库（思源黑体子集，OFL）；源码体积大，单独放 fonts/
         "fonts/lv_font_wb_14.c"
         "fonts/lv_font_wb_16.c"
    INCLUDE_DIRS "."
    REQUIRES bsp bt esp_event esp_hw_support esp_netif esp_timer esp_wifi nvs_flash
)
