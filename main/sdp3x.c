#include "sdp3x.h"
#include "driver/gpio.h"
#include "driver/i2c_master.h"
#include "esp_check.h"
#include "esp_log.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "sdkconfig.h"

#ifndef CONFIG_DRONE_SENSOR_DEBUG_MODE
#define CONFIG_DRONE_SENSOR_DEBUG_MODE 0
#endif

#define TAG "sdp3x"
static i2c_master_bus_handle_t bus;
static i2c_master_dev_handle_t sensor;
#if !CONFIG_DRONE_SENSOR_DEBUG_MODE
static i2c_master_dev_handle_t mux;
#endif
static esp_err_t start_errors[SENSOR_COUNT];
static bool start_attempted[SENSOR_COUNT];
static sensor_bus_diag_t bus_diag={
    .sda_gpio=CONFIG_DRONE_I2C_SDA_GPIO,
    .scl_gpio=CONFIG_DRONE_I2C_SCL_GPIO,
    .mux_detected_address=0xff,
    .mux_probe_error=ESP_ERR_NOT_FOUND,
};

static void refresh_line_levels(void){
    bus_diag.line_flags=(gpio_get_level(CONFIG_DRONE_I2C_SDA_GPIO)?1U:0U)|
                        (gpio_get_level(CONFIG_DRONE_I2C_SCL_GPIO)?2U:0U);
#if CONFIG_DRONE_SENSOR_DEBUG_MODE
    bus_diag.line_flags|=4U;
#endif
}

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
    vTaskDelay(pdMS_TO_TICKS(2));
    refresh_line_levels();
    for(uint8_t address=0x70;address<=0x77;address++){
        esp_err_t probe=i2c_master_probe(bus,address,CONFIG_DRONE_I2C_TIMEOUT_MS);
        bus_diag.mux_probe_error=(int32_t)probe;
        if(probe==ESP_OK){bus_diag.mux_detected_address=address;break;}
    }
    uint8_t mux_address=bus_diag.mux_detected_address==0xff?
                        CONFIG_DRONE_TCA9548A_ADDR:bus_diag.mux_detected_address;
    if(bus_diag.mux_detected_address==0xff){
        ESP_LOGE(TAG,"no TCA9548A at 0x70..0x77; SDA=GPIO%u level=%u, SCL=GPIO%u level=%u, probe=%s (%ld)",
                 bus_diag.sda_gpio,(unsigned)(bus_diag.line_flags&1U),bus_diag.scl_gpio,
                 (unsigned)((bus_diag.line_flags>>1)&1U),esp_err_to_name(bus_diag.mux_probe_error),
                 (long)bus_diag.mux_probe_error);
    }else{
        ESP_LOGI(TAG,"TCA9548A detected at 0x%02X; SDA=GPIO%u, SCL=GPIO%u, lines=%u/%u",
                 bus_diag.mux_detected_address,bus_diag.sda_gpio,bus_diag.scl_gpio,
                 (unsigned)(bus_diag.line_flags&1U),(unsigned)((bus_diag.line_flags>>1)&1U));
        if(mux_address!=CONFIG_DRONE_TCA9548A_ADDR)
            ESP_LOGW(TAG,"configured mux address 0x%02X replaced by detected address 0x%02X",
                     CONFIG_DRONE_TCA9548A_ADDR,mux_address);
    }
    i2c_device_config_t mc={.dev_addr_length=I2C_ADDR_BIT_LEN_7,.device_address=mux_address,.scl_speed_hz=CONFIG_DRONE_I2C_FREQ_HZ};
    ESP_RETURN_ON_ERROR(i2c_master_bus_add_device(bus,&mc,&mux),TAG,"add TCA9548A");
#else
    vTaskDelay(pdMS_TO_TICKS(2));
    refresh_line_levels();
    bus_diag.mux_probe_error=i2c_master_probe(bus,CONFIG_DRONE_SDP3X_ADDR,
                                              CONFIG_DRONE_I2C_TIMEOUT_MS);
    if(bus_diag.mux_probe_error==ESP_OK){
        bus_diag.mux_detected_address=CONFIG_DRONE_SDP3X_ADDR;
        ESP_LOGI(TAG,"direct SDP3x detected at 0x%02X; SDA=GPIO%u, SCL=GPIO%u, lines=%u/%u",
                 CONFIG_DRONE_SDP3X_ADDR,bus_diag.sda_gpio,bus_diag.scl_gpio,
                 (unsigned)(bus_diag.line_flags&1U),(unsigned)((bus_diag.line_flags>>1)&1U));
    }else{
        ESP_LOGE(TAG,"direct SDP3x not found at 0x%02X; SDA=GPIO%u level=%u, SCL=GPIO%u level=%u, probe=%s (%ld)",
                 CONFIG_DRONE_SDP3X_ADDR,bus_diag.sda_gpio,(unsigned)(bus_diag.line_flags&1U),
                 bus_diag.scl_gpio,(unsigned)((bus_diag.line_flags>>1)&1U),
                 esp_err_to_name(bus_diag.mux_probe_error),(long)bus_diag.mux_probe_error);
    }
#endif
    i2c_device_config_t sc={.dev_addr_length=I2C_ADDR_BIT_LEN_7,.device_address=CONFIG_DRONE_SDP3X_ADDR,.scl_speed_hz=CONFIG_DRONE_I2C_FREQ_HZ};
    return i2c_master_bus_add_device(bus,&sc,&sensor);
}

sensor_bus_diag_t sensors_get_bus_diagnostics(void){
    refresh_line_levels();
    return bus_diag;
}

static sensor_diag_t mux_failure_reason(void){
    refresh_line_levels();
    if((bus_diag.line_flags&3U)==0U)return SENSOR_DIAG_BUS_LINES_LOW;
    if(!(bus_diag.line_flags&1U))return SENSOR_DIAG_SDA_LOW;
    if(!(bus_diag.line_flags&2U))return SENSOR_DIAG_SCL_LOW;
    if(bus_diag.mux_detected_address==0xff)return SENSOR_DIAG_MUX_NOT_FOUND;
    return SENSOR_DIAG_MUX_SELECT;
}

static void mark_not_enabled(sensor_sample_t *sample){
    sample->pressure_pa=0.0f;
    sample->temperature_c=0.0f;
    sample->scale_factor=0;
    sample->valid=false;
    sample->diagnostic=SENSOR_DIAG_NOT_ENABLED;
    sample->last_error=ESP_OK;
}
esp_err_t sensors_start(void) {
    // Match the earliest firmware: continuous differential-pressure
    // measurement with sensor-side averaging until each read (0x3615).
    // Keep the host output rate independent so the current 200 Hz protocol,
    // CSV schema and inference window remain compatible.
    const uint8_t cmd[]={0x36,0x15};
    const uint8_t count=CONFIG_DRONE_SENSOR_DEBUG_MODE?1:SENSOR_COUNT;
    esp_err_t overall=ESP_OK;
    for(uint8_t ch=0;ch<count;ch++){
        bool first=!start_attempted[ch];
        esp_err_t previous=start_errors[ch];
        esp_err_t err=select_channel(ch);
        if(err==ESP_OK) err=i2c_master_transmit(sensor,cmd,sizeof(cmd),CONFIG_DRONE_I2C_TIMEOUT_MS);
        start_attempted[ch]=true;
        start_errors[ch]=err;
        if(err!=ESP_OK){
            if(first||previous!=err)
                ESP_LOGE(TAG,"channel %u start failed: %s (%ld)",ch+1,
                         esp_err_to_name(err),(long)err);
            overall=err;
        } else if(first||previous!=ESP_OK) {
            ESP_LOGI(TAG,"channel %u continuous measurement started",ch+1);
        }
    }
    vTaskDelay(pdMS_TO_TICKS(20));
    return overall;
}

static void mark_invalid(sensor_sample_t *sample,uint8_t ch,sensor_diag_t reason,
                         esp_err_t error)
{
    uint8_t previous=sample->diagnostic;
    sample->pressure_pa=0.0f;
    sample->temperature_c=0.0f;
    sample->scale_factor=0;
    sample->valid=false;
    sample->diagnostic=(uint8_t)reason;
    sample->last_error=(int32_t)error;
    sample->errors++;
    if(previous!=(uint8_t)reason||sample->errors==1||sample->errors%200U==0){
        ESP_LOGE(TAG,"channel %u invalid: reason=%u error=%s (%ld), count=%lu",
                 ch+1,(unsigned)reason,esp_err_to_name(error),(long)error,
                 (unsigned long)sample->errors);
    }
}

esp_err_t sensors_read_all(sensor_sample_t samples[SENSOR_COUNT]) {
    esp_err_t overall=ESP_OK;
    const uint8_t count=CONFIG_DRONE_SENSOR_DEBUG_MODE?1:SENSOR_COUNT;
    for(uint8_t ch=count;ch<SENSOR_COUNT;ch++)mark_not_enabled(&samples[ch]);
    for(uint8_t ch=0;ch<count;ch++){
        uint8_t d[9]={}; samples[ch].valid=false;
        esp_err_t e=select_channel(ch);
        if(e!=ESP_OK){mark_invalid(&samples[ch],ch,mux_failure_reason(),e);overall=e;continue;}
        e=i2c_master_receive(sensor,d,sizeof(d),CONFIG_DRONE_I2C_TIMEOUT_MS);
        if(e!=ESP_OK){
            sensor_diag_t reason=start_errors[ch]==ESP_OK?SENSOR_DIAG_READ:SENSOR_DIAG_START_COMMAND;
            esp_err_t detail=start_errors[ch]==ESP_OK?e:start_errors[ch];
            mark_invalid(&samples[ch],ch,reason,detail);overall=detail;continue;
        }
        if(crc8(d,2)!=d[2]){mark_invalid(&samples[ch],ch,SENSOR_DIAG_PRESSURE_CRC,ESP_ERR_INVALID_CRC);overall=ESP_ERR_INVALID_CRC;continue;}
        if(crc8(d+3,2)!=d[5]){mark_invalid(&samples[ch],ch,SENSOR_DIAG_TEMPERATURE_CRC,ESP_ERR_INVALID_CRC);overall=ESP_ERR_INVALID_CRC;continue;}
        if(crc8(d+6,2)!=d[8]){mark_invalid(&samples[ch],ch,SENSOR_DIAG_SCALE_CRC,ESP_ERR_INVALID_CRC);overall=ESP_ERR_INVALID_CRC;continue;}
        int16_t rp=(int16_t)((d[0]<<8)|d[1]),rt=(int16_t)((d[3]<<8)|d[4]);uint16_t scale=(uint16_t)((d[6]<<8)|d[7]);
        if(!scale){mark_invalid(&samples[ch],ch,SENSOR_DIAG_SCALE_ZERO,ESP_ERR_INVALID_RESPONSE);overall=ESP_ERR_INVALID_RESPONSE;continue;}
        samples[ch].pressure_pa=(float)rp/scale;samples[ch].temperature_c=(float)rt/200.0f;samples[ch].scale_factor=scale;samples[ch].valid=true;samples[ch].diagnostic=SENSOR_DIAG_OK;samples[ch].last_error=ESP_OK;
    } return overall;
}
