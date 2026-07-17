#include "sdp3x.h"
#include "driver/i2c_master.h"
#include "esp_check.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "sdkconfig.h"

#ifndef CONFIG_DRONE_SENSOR_DEBUG_MODE
#define CONFIG_DRONE_SENSOR_DEBUG_MODE 0
#endif

#define TAG "sdp3x"
static i2c_master_bus_handle_t bus;
static i2c_master_dev_handle_t mux, sensor;

static uint8_t crc8(const uint8_t *data, size_t len) {
    uint8_t crc=0xff; while(len--){crc^=*data++;for(int i=0;i<8;i++)crc=(crc&0x80)?(uint8_t)((crc<<1)^0x31):(uint8_t)(crc<<1);} return crc;
}
static esp_err_t select_channel(uint8_t ch) {
#if CONFIG_DRONE_SENSOR_DEBUG_MODE
    (void)ch;
    return ESP_OK;
#else
    uint8_t mask=(uint8_t)(1U<<ch);
    return i2c_master_transmit(mux,&mask,1,CONFIG_DRONE_I2C_TIMEOUT_MS);
#endif
}

esp_err_t sensors_init(void) {
    i2c_master_bus_config_t cfg={.i2c_port=I2C_NUM_0,.sda_io_num=CONFIG_DRONE_I2C_SDA_GPIO,.scl_io_num=CONFIG_DRONE_I2C_SCL_GPIO,.clk_source=I2C_CLK_SRC_DEFAULT,.glitch_ignore_cnt=7,.flags.enable_internal_pullup=true};
    ESP_RETURN_ON_ERROR(i2c_new_master_bus(&cfg,&bus),TAG,"create I2C bus");
#if !CONFIG_DRONE_SENSOR_DEBUG_MODE
    i2c_device_config_t mc={.dev_addr_length=I2C_ADDR_BIT_LEN_7,.device_address=CONFIG_DRONE_TCA9548A_ADDR,.scl_speed_hz=CONFIG_DRONE_I2C_FREQ_HZ};
    ESP_RETURN_ON_ERROR(i2c_master_bus_add_device(bus,&mc,&mux),TAG,"add TCA9548A");
#endif
    i2c_device_config_t sc={.dev_addr_length=I2C_ADDR_BIT_LEN_7,.device_address=CONFIG_DRONE_SDP3X_ADDR,.scl_speed_hz=CONFIG_DRONE_I2C_FREQ_HZ};
    return i2c_master_bus_add_device(bus,&sc,&sensor);
}
esp_err_t sensors_start(void) {
    const uint8_t cmd[]={0x36,0x15};
    const uint8_t count=CONFIG_DRONE_SENSOR_DEBUG_MODE?1:SENSOR_COUNT;
    esp_err_t overall=ESP_OK;
    for(uint8_t ch=0;ch<count;ch++){
        esp_err_t err=select_channel(ch);
        if(err==ESP_OK) err=i2c_master_transmit(sensor,cmd,sizeof(cmd),CONFIG_DRONE_I2C_TIMEOUT_MS);
        if(err!=ESP_OK) overall=err;
    }
    vTaskDelay(pdMS_TO_TICKS(20));
    return overall;
}
esp_err_t sensors_read_all(sensor_sample_t samples[SENSOR_COUNT]) {
    esp_err_t overall=ESP_OK;
    const uint8_t count=CONFIG_DRONE_SENSOR_DEBUG_MODE?1:SENSOR_COUNT;
    for(uint8_t ch=count;ch<SENSOR_COUNT;ch++) samples[ch].valid=false;
    for(uint8_t ch=0;ch<count;ch++){
        uint8_t d[9]={}; samples[ch].valid=false; esp_err_t e=select_channel(ch); if(e==ESP_OK)e=i2c_master_receive(sensor,d,sizeof(d),CONFIG_DRONE_I2C_TIMEOUT_MS);
        if(e!=ESP_OK||crc8(d,2)!=d[2]||crc8(d+3,2)!=d[5]||crc8(d+6,2)!=d[8]){samples[ch].errors++;overall=e==ESP_OK?ESP_ERR_INVALID_CRC:e;continue;}
        int16_t rp=(int16_t)((d[0]<<8)|d[1]),rt=(int16_t)((d[3]<<8)|d[4]);uint16_t scale=(uint16_t)((d[6]<<8)|d[7]);
        if(!scale){samples[ch].errors++;overall=ESP_ERR_INVALID_RESPONSE;continue;}
        samples[ch].pressure_pa=(float)rp/scale;samples[ch].temperature_c=(float)rt/200.0f;samples[ch].scale_factor=scale;samples[ch].valid=true;
    } return overall;
}
