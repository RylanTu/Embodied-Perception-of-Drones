#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "esp_log.h"
#include "esp_timer.h"
#include "protocol.h"
#include "sdp3x.h"
#include "sensor_filter.h"
#include "servo.h"
#include "sdkconfig.h"
#define TAG "main"
void app_main(void){
    protocol_init();
    ESP_ERROR_CHECK(servo_init());
#if CONFIG_DRONE_SENSOR_DEBUG_MODE && CONFIG_DRONE_SERVO_MODE_PULSE
    ESP_ERROR_CHECK(servo_start_test_wave());
    ESP_LOGI(TAG,"GPIO%d test wave: %d Hz, 50%% duty",CONFIG_DRONE_SERVO_PULSE_GPIO,CONFIG_DRONE_SERVO_DEFAULT_FREQ_HZ);
#endif
    ESP_ERROR_CHECK(sensors_init());
    sensor_filter_init();
    esp_err_t sensor_start_result=sensors_start();
    if(sensor_start_result!=ESP_OK) ESP_LOGE(TAG,"SDP3x start failed: %s; will retry",esp_err_to_name(sensor_start_result));
    TickType_t period=pdMS_TO_TICKS(1000/CONFIG_DRONE_SAMPLE_RATE_HZ),next=xTaskGetTickCount();
    TickType_t next_retry=xTaskGetTickCount()+pdMS_TO_TICKS(1000);
    sensor_sample_t samples[SENSOR_COUNT]={};
    while(1){
        protocol_poll();
        TickType_t now=xTaskGetTickCount();
        if(sensor_start_result!=ESP_OK&&(int32_t)(now-next_retry)>=0){
            sensor_start_result=sensors_start();
            if(sensor_start_result==ESP_OK) ESP_LOGI(TAG,"SDP3x started");
            else ESP_LOGE(TAG,"SDP3x retry failed: %s",esp_err_to_name(sensor_start_result));
            next_retry=now+pdMS_TO_TICKS(1000);
        }
        if((int32_t)(now-next)>=0){
            sensors_read_all(samples);
            sensor_filter_apply(samples);
            protocol_send_samples((uint32_t)(esp_timer_get_time()/1000),samples);
            next+=period;
        }
        vTaskDelay(pdMS_TO_TICKS(1));
    }
}
