idf_component_register(SRCS "wifi_web.c" "capture_usb.c" "main.c" "sdp3x.c" "sensor_filter.c" "sensor_test.c" "servo.c" "protocol.c"
                       REQUIRES driver esp_driver_gpio esp_driver_i2c esp_driver_rmt
                                esp_driver_uart esp_driver_usb_serial_jtag esp_timer
                                esp_wifi esp_netif esp_event esp_http_server nvs_flash
                       INCLUDE_DIRS ""
                       EMBED_FILES "web_wifi/index.html" "web_wifi/app.js" "web_wifi/style.css")
