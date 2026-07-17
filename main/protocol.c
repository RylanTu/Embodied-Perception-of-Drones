#include "protocol.h"
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <strings.h>
#include "driver/usb_serial_jtag.h"
#include "freertos/FreeRTOS.h"
#include "servo.h"
#include "sdkconfig.h"
static output_format_t format=CONFIG_DRONE_OUTPUT_FORMAT;static char input[160];static size_t input_len;
static void send_bytes(const void*d,size_t n){usb_serial_jtag_write_bytes(d,n,pdMS_TO_TICKS(20));}static void send_text(const char*s){send_bytes(s,strlen(s));}
static void reply(esp_err_t e){char b[64];snprintf(b,sizeof(b),e==ESP_OK?"OK\r\n":"ERR %s\r\n",esp_err_to_name(e));send_text(b);}
static void handle(char*line){char*av[8]={};int ac=0;for(char*p=strtok(line," ,\t");p&&ac<8;p=strtok(NULL," ,\t"))av[ac++]=p;if(!ac)return;
    if(!strcasecmp(av[0],"HELP")){
        send_text("OK COMMANDS: ENABLE 0|1, MOVE pulses dir hz accel_ms decel_ms, STOP, CLEAR, STATUS, FORMAT CSV|JSON|BINARY, MBREAD reg count, MBWRITE reg value\r\n");
        return;
    }
    if(!strcasecmp(av[0],"FORMAT")&&ac==2){if(!strcasecmp(av[1],"CSV"))format=OUTPUT_CSV;else if(!strcasecmp(av[1],"JSON"))format=OUTPUT_JSON;else if(!strcasecmp(av[1],"BINARY"))format=OUTPUT_BINARY;else{reply(ESP_ERR_INVALID_ARG);return;}reply(ESP_OK);return;}
    if(!strcasecmp(av[0],"ENABLE")&&ac==2){reply(servo_enable(atoi(av[1])!=0));return;}if(!strcasecmp(av[0],"MOVE")&&ac==6){reply(servo_move(strtoul(av[1],0,0),atoi(av[2])!=0,strtoul(av[3],0,0),strtoul(av[4],0,0),strtoul(av[5],0,0)));return;}
    if(!strcasecmp(av[0],"STOP")){servo_emergency_stop();reply(ESP_OK);return;}if(!strcasecmp(av[0],"CLEAR")){servo_clear_emergency();reply(ESP_OK);return;}
    if(!strcasecmp(av[0],"STATUS")){servo_status_t s=servo_get_status();char b[160];snprintf(b,sizeof(b),"STATUS mode=%s enabled=%u estop=%u moving=%u dir=%u hz=%lu remaining=%lu\r\n",s.mode?"modbus":"pulse",s.enabled,s.emergency_stop,s.moving,s.direction,(unsigned long)s.frequency_hz,(unsigned long)s.pulses_remaining);send_text(b);return;}
    if(!strcasecmp(av[0],"MBWRITE")&&ac==3){reply(modbus_write_single(strtoul(av[1],0,0),strtoul(av[2],0,0)));return;}if(!strcasecmp(av[0],"MBREAD")&&ac==3){uint16_t v[16],n=strtoul(av[2],0,0);esp_err_t e=modbus_read_holding(strtoul(av[1],0,0),n,v);if(e!=ESP_OK){reply(e);return;}char b[160],*p=b;p+=snprintf(p,sizeof(b),"MB");for(int i=0;i<n;i++)p+=snprintf(p,sizeof(b)-(p-b)," %u",v[i]);snprintf(p,sizeof(b)-(p-b),"\r\n");send_text(b);return;}reply(ESP_ERR_INVALID_ARG);
}
void protocol_init(void){usb_serial_jtag_driver_config_t c={.tx_buffer_size=4096,.rx_buffer_size=512};ESP_ERROR_CHECK(usb_serial_jtag_driver_install(&c));
#if CONFIG_DRONE_SENSOR_DEBUG_MODE
send_text("READY sensor-debug v1\r\n");
#else
send_text("READY drone-controller v1\r\n");
#endif
}
void protocol_poll(void){uint8_t b[64];int n=usb_serial_jtag_read_bytes(b,sizeof(b),0);for(int i=0;i<n;i++){if(b[i]=='\r'||b[i]=='\n'){if(input_len){input[input_len]=0;handle(input);input_len=0;}}else if(input_len<sizeof(input)-1)input[input_len++]=b[i];else input_len=0;}}
typedef struct __attribute__((packed)){uint16_t magic;uint8_t version,count;uint32_t ms;int32_t pressure_mpa[SENSOR_COUNT];int16_t temp_cc[SENSOR_COUNT];uint8_t valid_mask;uint16_t crc;}packet_t;
static uint16_t crc16(const uint8_t*p,size_t n){uint16_t c=0xffff;while(n--){c^=*p++;for(int i=0;i<8;i++)c=(c&1)?(c>>1)^0xa001:c>>1;}return c;}
void protocol_send_samples(uint32_t ms,const sensor_sample_t s[SENSOR_COUNT]){char b[512];if(format==OUTPUT_CSV){int n=snprintf(b,sizeof(b),"DATA,%lu",(unsigned long)ms);for(int i=0;i<SENSOR_COUNT;i++)n+=snprintf(b+n,sizeof(b)-n,",%.3f,%.2f,%u",s[i].pressure_pa,s[i].temperature_c,s[i].valid);snprintf(b+n,sizeof(b)-n,"\r\n");send_text(b);}else if(format==OUTPUT_JSON){int n=snprintf(b,sizeof(b),"{\"type\":\"data\",\"ms\":%lu,\"sensors\":[",(unsigned long)ms);for(int i=0;i<SENSOR_COUNT;i++)n+=snprintf(b+n,sizeof(b)-n,"%s{\"p\":%.3f,\"t\":%.2f,\"ok\":%s}",i?",":"",s[i].pressure_pa,s[i].temperature_c,s[i].valid?"true":"false");snprintf(b+n,sizeof(b)-n,"]}\r\n");send_text(b);}else{packet_t p={.magic=0xa55a,.version=1,.count=SENSOR_COUNT,.ms=ms};for(int i=0;i<SENSOR_COUNT;i++){p.pressure_mpa[i]=(int32_t)(s[i].pressure_pa*1000);p.temp_cc[i]=(int16_t)(s[i].temperature_c*100);if(s[i].valid)p.valid_mask|=1<<i;}p.crc=crc16((uint8_t*)&p,sizeof(p)-2);send_bytes(&p,sizeof(p));}}
