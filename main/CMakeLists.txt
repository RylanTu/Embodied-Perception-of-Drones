idf_component_register(SRCS "main.c" "sdp3x.c" "servo.c" "protocol.c"
                       REQUIRES driver esp_driver_gpio esp_driver_i2c esp_driver_rmt
                                esp_driver_uart esp_driver_usb_serial_jtag esp_timer
                       INCLUDE_DIRS "")
