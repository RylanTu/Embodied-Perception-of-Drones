#include "servo.h"
#include <stdlib.h>
#include "driver/gpio.h"
#include "driver/rmt_tx.h"
#include "driver/uart.h"
#include "esp_check.h"
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"
#include "freertos/task.h"
#include "sdkconfig.h"

#ifndef CONFIG_DRONE_SERVO_DIR_INVERT
#define CONFIG_DRONE_SERVO_DIR_INVERT 0
#endif
#ifndef CONFIG_DRONE_SERVO_ENABLE_ACTIVE_LOW
#define CONFIG_DRONE_SERVO_ENABLE_ACTIVE_LOW 0
#endif

#define TAG "servo"
#define RMT_RESOLUTION_HZ 1000000U
#define CHUNK_PULSES 64U
static servo_status_t status={.mode=CONFIG_DRONE_SERVO_MODE,.frequency_hz=CONFIG_DRONE_SERVO_DEFAULT_FREQ_HZ};
static SemaphoreHandle_t lock;
static rmt_channel_handle_t rmt_channel;
static rmt_encoder_handle_t rmt_encoder;
static volatile bool stop_requested;
static bool test_wave_active;

static uint16_t modbus_crc(const uint8_t *d,size_t n){uint16_t c=0xffff;while(n--){c^=*d++;for(int i=0;i<8;i++)c=(c&1)?(c>>1)^0xa001:c>>1;}return c;}
static esp_err_t modbus_exchange(uint8_t *req,size_t n,uint8_t *resp,size_t cap,size_t *out){
    if(status.mode!=SERVO_MODBUS) return ESP_ERR_NOT_SUPPORTED;
    uint16_t crc=modbus_crc(req,n);
    req[n++]=crc; req[n++]=crc>>8;
    uart_flush_input(CONFIG_DRONE_RS485_UART_NUM);
    if(uart_write_bytes(CONFIG_DRONE_RS485_UART_NUM,req,n)!=(int)n) return ESP_FAIL;
    uart_wait_tx_done(CONFIG_DRONE_RS485_UART_NUM,pdMS_TO_TICKS(CONFIG_DRONE_MODBUS_TIMEOUT_MS));
    int got=uart_read_bytes(CONFIG_DRONE_RS485_UART_NUM,resp,cap,pdMS_TO_TICKS(CONFIG_DRONE_MODBUS_TIMEOUT_MS));
    if(got<5) return ESP_ERR_TIMEOUT;
    uint16_t rcrc=(uint16_t)resp[got-2]|((uint16_t)resp[got-1]<<8);
    if(rcrc!=modbus_crc(resp,got-2)) return ESP_ERR_INVALID_CRC;
    if(resp[0]!=CONFIG_DRONE_MODBUS_SLAVE_ADDR||(resp[1]&0x80)) return ESP_ERR_INVALID_RESPONSE;
    *out=got;
    return ESP_OK;
}
esp_err_t modbus_write_single(uint16_t reg,uint16_t value){uint8_t q[8]={CONFIG_DRONE_MODBUS_SLAVE_ADDR,6,reg>>8,reg,value>>8,value},r[8];size_t n;return modbus_exchange(q,6,r,sizeof(r),&n);}
esp_err_t modbus_read_holding(uint16_t reg,uint16_t count,uint16_t *values){if(!count||count>16)return ESP_ERR_INVALID_ARG;uint8_t q[8]={CONFIG_DRONE_MODBUS_SLAVE_ADDR,3,reg>>8,reg,count>>8,count},r[37];size_t n;ESP_RETURN_ON_ERROR(modbus_exchange(q,6,r,sizeof(r),&n),TAG,"read");if(n!=5+count*2||r[2]!=count*2)return ESP_ERR_INVALID_SIZE;for(int i=0;i<count;i++)values[i]=((uint16_t)r[3+i*2]<<8)|r[4+i*2];return ESP_OK;}
static void update_moving(bool moving,uint32_t remaining){xSemaphoreTake(lock,portMAX_DELAY);status.moving=moving;status.pulses_remaining=remaining;xSemaphoreGive(lock);}
typedef struct{uint32_t pulses,freq,accel_ms,decel_ms;bool dir;}move_args_t;
static void pulse_task(void *arg){
    move_args_t a=*(move_args_t*)arg;free(arg);gpio_set_level(CONFIG_DRONE_SERVO_DIR_GPIO,a.dir^CONFIG_DRONE_SERVO_DIR_INVERT);vTaskDelay(pdMS_TO_TICKS(1));
    uint32_t done=0,ap=(uint32_t)(((uint64_t)a.freq*a.accel_ms)/2000U),dp=(uint32_t)(((uint64_t)a.freq*a.decel_ms)/2000U);if(ap+dp>a.pulses){ap=a.pulses/2;dp=a.pulses-ap;}
    while(done<a.pulses&&!stop_requested){uint32_t count=a.pulses-done>CHUNK_PULSES?CHUNK_PULSES:a.pulses-done,mid=done+count/2,f=a.freq,min=CONFIG_DRONE_SERVO_MIN_FREQ_HZ,left=a.pulses-mid;
        if(ap&&mid<ap) f=min+(uint32_t)(((uint64_t)(a.freq-min)*mid)/ap);
        if(dp&&left<dp) f=min+(uint32_t)(((uint64_t)(a.freq-min)*left)/dp);
        if(f<min) f=min;
        uint32_t half=RMT_RESOLUTION_HZ/(2*f);
        if(!half) half=1;
        rmt_symbol_word_t symbols[CHUNK_PULSES];for(uint32_t i=0;i<count;i++)symbols[i]=(rmt_symbol_word_t){.level0=1,.duration0=half,.level1=0,.duration1=half};rmt_transmit_config_t tx={};
        if(rmt_transmit(rmt_channel,rmt_encoder,symbols,count*sizeof(symbols[0]),&tx)!=ESP_OK||rmt_tx_wait_all_done(rmt_channel,-1)!=ESP_OK) break;
        done+=count;
        update_moving(true,a.pulses-done);
    }update_moving(false,0);vTaskDelete(NULL);
}
esp_err_t servo_init(void){
    lock=xSemaphoreCreateMutex();if(!lock)return ESP_ERR_NO_MEM;gpio_config_t o={.pin_bit_mask=(1ULL<<CONFIG_DRONE_SERVO_DIR_GPIO)|(1ULL<<CONFIG_DRONE_SERVO_ENABLE_GPIO),.mode=GPIO_MODE_OUTPUT};ESP_RETURN_ON_ERROR(gpio_config(&o),TAG,"GPIO");servo_enable(false);
    if(status.mode==SERVO_PULSE){rmt_tx_channel_config_t tc={.gpio_num=CONFIG_DRONE_SERVO_PULSE_GPIO,.clk_src=RMT_CLK_SRC_DEFAULT,.resolution_hz=RMT_RESOLUTION_HZ,.mem_block_symbols=64,.trans_queue_depth=2};ESP_RETURN_ON_ERROR(rmt_new_tx_channel(&tc,&rmt_channel),TAG,"RMT");rmt_copy_encoder_config_t ec={};ESP_RETURN_ON_ERROR(rmt_new_copy_encoder(&ec,&rmt_encoder),TAG,"encoder");return rmt_enable(rmt_channel);}
    uart_config_t uc={.baud_rate=CONFIG_DRONE_RS485_BAUD_RATE,.data_bits=UART_DATA_8_BITS,.parity=CONFIG_DRONE_RS485_PARITY,.stop_bits=CONFIG_DRONE_RS485_STOP_BITS,.flow_ctrl=UART_HW_FLOWCTRL_DISABLE,.source_clk=UART_SCLK_DEFAULT};ESP_RETURN_ON_ERROR(uart_driver_install(CONFIG_DRONE_RS485_UART_NUM,256,0,0,NULL,0),TAG,"UART");ESP_RETURN_ON_ERROR(uart_param_config(CONFIG_DRONE_RS485_UART_NUM,&uc),TAG,"UART config");ESP_RETURN_ON_ERROR(uart_set_pin(CONFIG_DRONE_RS485_UART_NUM,CONFIG_DRONE_RS485_TX_GPIO,CONFIG_DRONE_RS485_RX_GPIO,CONFIG_DRONE_RS485_DE_GPIO,UART_PIN_NO_CHANGE),TAG,"pins");return uart_set_mode(CONFIG_DRONE_RS485_UART_NUM,UART_MODE_RS485_HALF_DUPLEX);
}
esp_err_t servo_start_test_wave(void){
    if(status.mode!=SERVO_PULSE) return ESP_ERR_NOT_SUPPORTED;
    if(status.moving||test_wave_active) return ESP_ERR_INVALID_STATE;
    uint32_t half=RMT_RESOLUTION_HZ/(2U*CONFIG_DRONE_SERVO_DEFAULT_FREQ_HZ);
    if(!half) half=1;
    rmt_symbol_word_t symbol={.level0=1,.duration0=half,.level1=0,.duration1=half};
    rmt_transmit_config_t tx={.loop_count=-1};
    esp_err_t err=rmt_transmit(rmt_channel,rmt_encoder,&symbol,sizeof(symbol),&tx);
    if(err==ESP_OK) test_wave_active=true;
    return err;
}
esp_err_t servo_stop_test_wave(void){
    if(!test_wave_active) return ESP_OK;
    ESP_RETURN_ON_ERROR(rmt_disable(rmt_channel),TAG,"stop test wave");
    ESP_RETURN_ON_ERROR(rmt_enable(rmt_channel),TAG,"restart RMT channel");
    test_wave_active=false;
    return ESP_OK;
}
esp_err_t servo_enable(bool en){if(status.emergency_stop&&en)return ESP_ERR_INVALID_STATE;gpio_set_level(CONFIG_DRONE_SERVO_ENABLE_GPIO,en^CONFIG_DRONE_SERVO_ENABLE_ACTIVE_LOW);status.enabled=en;return ESP_OK;}
esp_err_t servo_move(uint32_t pulses,bool dir,uint32_t freq,uint32_t accel,uint32_t decel){if(status.mode!=SERVO_PULSE)return ESP_ERR_NOT_SUPPORTED;if(!status.enabled||status.emergency_stop||status.moving||!pulses||freq<CONFIG_DRONE_SERVO_MIN_FREQ_HZ||freq>CONFIG_DRONE_SERVO_MAX_FREQ_HZ)return ESP_ERR_INVALID_STATE;ESP_RETURN_ON_ERROR(servo_stop_test_wave(),TAG,"stop test wave");move_args_t*a=malloc(sizeof(*a));if(!a)return ESP_ERR_NO_MEM;*a=(move_args_t){pulses,freq,accel,decel,dir};stop_requested=false;status.direction=dir;status.frequency_hz=freq;update_moving(true,pulses);if(xTaskCreate(pulse_task,"servo_move",4096,a,8,NULL)!=pdPASS){free(a);update_moving(false,0);return ESP_ERR_NO_MEM;}return ESP_OK;}
void servo_emergency_stop(void){stop_requested=true;servo_stop_test_wave();status.emergency_stop=true;servo_enable(false);}void servo_clear_emergency(void){if(!status.moving)status.emergency_stop=false;}servo_status_t servo_get_status(void){xSemaphoreTake(lock,portMAX_DELAY);servo_status_t s=status;xSemaphoreGive(lock);return s;}
