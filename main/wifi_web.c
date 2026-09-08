#include "wifi_web.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <strings.h>

#include "esp_event.h"
#include "esp_check.h"
#include "esp_http_server.h"
#include "esp_log.h"
#include "esp_netif.h"
#include "esp_wifi.h"
#include "freertos/FreeRTOS.h"
#include "freertos/queue.h"
#include "freertos/semphr.h"
#include "freertos/task.h"
#include "nvs_flash.h"
#include "sdp3x.h"
#include "sensor_test.h"
#include "servo.h"
#include "sdkconfig.h"

#define TAG "wifi-web"
#define COMMAND_BYTES 256

extern const uint8_t index_html_start[] asm("_binary_index_html_start");
extern const uint8_t index_html_end[] asm("_binary_index_html_end");
extern const uint8_t app_js_start[] asm("_binary_app_js_start");
extern const uint8_t app_js_end[] asm("_binary_app_js_end");
extern const uint8_t style_css_start[] asm("_binary_style_css_start");
extern const uint8_t style_css_end[] asm("_binary_style_css_end");

static httpd_handle_t server;
static volatile int websocket_fd = -1;
static uint32_t sample_sequence;
static SemaphoreHandle_t send_lock;
static QueueHandle_t sample_queue;

typedef struct {
    uint32_t uptime_ms;
    uint32_t sequence;
    sensor_sample_t samples[SENSOR_COUNT];
} queued_sample_t;

static esp_err_t asset(httpd_req_t *request, const char *type,
                       const uint8_t *start, const uint8_t *end)
{
    httpd_resp_set_type(request, type);
    httpd_resp_set_hdr(request, "Cache-Control", "no-store");
    return httpd_resp_send(request, (const char *)start, end - start);
}

static esp_err_t index_handler(httpd_req_t *request)
{
    return asset(request, "text/html; charset=utf-8", index_html_start, index_html_end);
}

static esp_err_t app_handler(httpd_req_t *request)
{
    return asset(request, "application/javascript; charset=utf-8", app_js_start, app_js_end);
}

static esp_err_t style_handler(httpd_req_t *request)
{
    return asset(request, "text/css; charset=utf-8", style_css_start, style_css_end);
}

static esp_err_t send_text(httpd_req_t *request, const char *text)
{
    httpd_ws_frame_t frame = {
        .type = HTTPD_WS_TYPE_TEXT,
        .payload = (uint8_t *)text,
        .len = strlen(text),
    };
    xSemaphoreTake(send_lock, portMAX_DELAY);
    esp_err_t result = httpd_ws_send_frame(request, &frame);
    xSemaphoreGive(send_lock);
    return result;
}

static esp_err_t reply(httpd_req_t *request, const char *command, esp_err_t result)
{
    char text[192];
    snprintf(text, sizeof(text),
             "{\"type\":\"reply\",\"command\":\"%s\",\"ok\":%s,"
             "\"code\":%ld,\"message\":\"%s\"}",
             command, result == ESP_OK ? "true" : "false", (long)result,
             result == ESP_OK ? "OK" : esp_err_to_name(result));
    return send_text(request, text);
}

static esp_err_t handle_command(httpd_req_t *request, char *line)
{
    char *argv[10] = {0};
    int argc = 0;
    for (char *token = strtok(line, " ,\t\r\n"); token && argc < 10;
         token = strtok(NULL, " ,\t\r\n")) {
        argv[argc++] = token;
    }
    if (!argc) return ESP_OK;
    websocket_fd = httpd_req_to_sockfd(request);

    if (!strcasecmp(argv[0], "HELLO")) {
        servo_link_heartbeat();
        return send_text(request,
                         "{\"type\":\"hello\",\"protocol\":2,"
                         "\"device\":\"drone-esp32s3-wifi\",\"sensors\":5}");
    }
    if (!strcasecmp(argv[0], "HEARTBEAT")) {
        servo_link_heartbeat();
        return ESP_OK;
    }
    servo_link_heartbeat();
    if (!strcasecmp(argv[0], "ENABLE") && argc == 2) {
        return reply(request, "ENABLE", servo_enable(atoi(argv[1]) != 0));
    }
    if (!strcasecmp(argv[0], "STOP")) {
        servo_emergency_stop();
        return reply(request, "STOP", ESP_OK);
    }
    if (!strcasecmp(argv[0], "CLEAR")) {
        servo_clear_emergency();
        return reply(request, "CLEAR", ESP_OK);
    }
    if (!strcasecmp(argv[0], "CONFIG") && argc == 5) {
        float min_mm = strtof(argv[1], NULL);
        float max_mm = strtof(argv[2], NULL);
        float pulses_per_mm = strtof(argv[3], NULL);
        float max_speed_mm_s = strtof(argv[4], NULL);
        esp_err_t result = servo_configure_limits(
            (int32_t)(min_mm * 1000.0f), (int32_t)(max_mm * 1000.0f),
            (uint32_t)(pulses_per_mm * 1000.0f),
            (uint32_t)(max_speed_mm_s * 1000.0f));
        return reply(request, "CONFIG", result);
    }
    if (!strcasecmp(argv[0], "MOVE_MM") && argc == 5) {
        return reply(request, "MOVE_MM", servo_move_distance_text(argv[1], argv[2], argv[3], argv[4]));
    }
    if (!strcasecmp(argv[0], "MOVE") && argc == 5) {
        esp_err_t result = servo_move_to_um(
            (int32_t)(strtof(argv[1], NULL) * 1000.0f),
            (uint32_t)(strtof(argv[2], NULL) * 1000.0f),
            strtoul(argv[3], NULL, 0), strtoul(argv[4], NULL, 0));
        return reply(request, "MOVE", result);
    }
    if (!strcasecmp(argv[0], "TEST") && argc == 6) {
        esp_err_t result = sensor_test_configure(
            atoi(argv[1]) != 0, (int32_t)(strtof(argv[2], NULL) * 1000.0f),
            (int32_t)(strtof(argv[3], NULL) * 1000.0f),
            strtoul(argv[4], NULL, 0), strtoul(argv[5], NULL, 0));
        return reply(request, "TEST", result);
    }
    return reply(request, argv[0], ESP_ERR_INVALID_ARG);
}

static esp_err_t websocket_handler(httpd_req_t *request)
{
    httpd_ws_frame_t frame = {0};
    esp_err_t result = httpd_ws_recv_frame(request, &frame, 0);
    if (result != ESP_OK) return result;
    if (frame.len == 0 || frame.len >= COMMAND_BYTES) return ESP_ERR_INVALID_SIZE;
    char input[COMMAND_BYTES];
    frame.payload = (uint8_t *)input;
    result = httpd_ws_recv_frame(request, &frame, sizeof(input) - 1);
    if (result != ESP_OK) return result;
    input[frame.len] = '\0';
    return frame.type == HTTPD_WS_TYPE_TEXT ? handle_command(request, input) : ESP_OK;
}

static esp_err_t start_server(void)
{
    httpd_config_t config = HTTPD_DEFAULT_CONFIG();
    config.stack_size = 8192;
    config.max_uri_handlers = 8;
    config.lru_purge_enable = true;
    ESP_RETURN_ON_ERROR(httpd_start(&server, &config), TAG, "start HTTP server");
    const httpd_uri_t routes[] = {
        {.uri = "/", .method = HTTP_GET, .handler = index_handler},
        {.uri = "/app.js", .method = HTTP_GET, .handler = app_handler},
        {.uri = "/style.css", .method = HTTP_GET, .handler = style_handler},
        {.uri = "/ws", .method = HTTP_GET, .handler = websocket_handler,
         .is_websocket = true},
    };
    for (size_t index = 0; index < sizeof(routes) / sizeof(routes[0]); index++) {
        ESP_RETURN_ON_ERROR(httpd_register_uri_handler(server, &routes[index]), TAG,
                            "register HTTP route");
    }
    return ESP_OK;
}

esp_err_t wifi_web_init(void)
{
    send_lock = xSemaphoreCreateMutex();
    sample_queue = xQueueCreate(16, sizeof(queued_sample_t));
    if (!send_lock || !sample_queue) return ESP_ERR_NO_MEM;
    esp_err_t result = nvs_flash_init();
    if (result == ESP_ERR_NVS_NO_FREE_PAGES || result == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        ESP_RETURN_ON_ERROR(nvs_flash_erase(), TAG, "erase NVS");
        result = nvs_flash_init();
    }
    ESP_RETURN_ON_ERROR(result, TAG, "init NVS");
    ESP_RETURN_ON_ERROR(esp_netif_init(), TAG, "init network interface");
    result = esp_event_loop_create_default();
    if (result != ESP_OK && result != ESP_ERR_INVALID_STATE) return result;
    esp_netif_create_default_wifi_ap();
    wifi_init_config_t init = WIFI_INIT_CONFIG_DEFAULT();
    ESP_RETURN_ON_ERROR(esp_wifi_init(&init), TAG, "init WiFi");
    wifi_config_t config = {0};
    strlcpy((char *)config.ap.ssid, CONFIG_DRONE_WIFI_AP_SSID,
            sizeof(config.ap.ssid));
    strlcpy((char *)config.ap.password, CONFIG_DRONE_WIFI_AP_PASSWORD,
            sizeof(config.ap.password));
    config.ap.ssid_len = strlen(CONFIG_DRONE_WIFI_AP_SSID);
    config.ap.channel = CONFIG_DRONE_WIFI_AP_CHANNEL;
    config.ap.max_connection = 2;
    config.ap.authmode = strlen(CONFIG_DRONE_WIFI_AP_PASSWORD) >= 8
                             ? WIFI_AUTH_WPA2_PSK : WIFI_AUTH_OPEN;
    config.ap.pmf_cfg.required = false;
    ESP_RETURN_ON_ERROR(esp_wifi_set_mode(WIFI_MODE_AP), TAG, "set AP mode");
    ESP_RETURN_ON_ERROR(esp_wifi_set_config(WIFI_IF_AP, &config), TAG, "set AP config");
    ESP_RETURN_ON_ERROR(esp_wifi_start(), TAG, "start AP");
    ESP_RETURN_ON_ERROR(start_server(), TAG, "start web UI");
    ESP_LOGI(TAG, "AP ready: SSID=%s URL=http://192.168.4.1",
             CONFIG_DRONE_WIFI_AP_SSID);
    return ESP_OK;
}

static void transmit_sample(const queued_sample_t *message)
{
    int fd = websocket_fd;
    if (!server || fd < 0 || httpd_ws_get_fd_info(server, fd) != HTTPD_WS_CLIENT_WEBSOCKET) {
        websocket_fd = -1;
        return;
    }
    servo_status_t status = servo_get_status();
    sensor_bus_diag_t bus_diag = sensors_get_bus_diagnostics();
    char text[640];
    int used = snprintf(text, sizeof(text),
                        "{\"type\":\"data\",\"ms\":%lu,\"seq\":%lu,\"p\":[",
                        (unsigned long)message->uptime_ms,
                        (unsigned long)message->sequence);
    for (int index = 0; index < SENSOR_COUNT; index++) {
        used += snprintf(text + used, sizeof(text) - (size_t)used, "%s%.4f",
                         index ? "," : "", message->samples[index].pressure_pa);
    }
    used += snprintf(text + used, sizeof(text) - (size_t)used, "],\"t\":[");
    uint8_t valid_mask = 0;
    for (int index = 0; index < SENSOR_COUNT; index++) {
        used += snprintf(text + used, sizeof(text) - (size_t)used, "%s%.2f",
                         index ? "," : "", message->samples[index].temperature_c);
        if (message->samples[index].valid) valid_mask |= 1U << index;
    }
    used += snprintf(text + used, sizeof(text) - (size_t)used, "],\"diag\":[");
    for (int index = 0; index < SENSOR_COUNT; index++)
        used += snprintf(text + used, sizeof(text) - (size_t)used, "%s%u", index ? "," : "",
                         (unsigned)message->samples[index].diagnostic);
    used += snprintf(text + used, sizeof(text) - (size_t)used, "],\"sensor_errors\":[");
    for (int index = 0; index < SENSOR_COUNT; index++)
        used += snprintf(text + used, sizeof(text) - (size_t)used, "%s%lu", index ? "," : "",
                         (unsigned long)message->samples[index].errors);
    used += snprintf(text + used, sizeof(text) - (size_t)used, "],\"i2c_errors\":[");
    for (int index = 0; index < SENSOR_COUNT; index++)
        used += snprintf(text + used, sizeof(text) - (size_t)used, "%s%ld", index ? "," : "",
                         (long)message->samples[index].last_error);
    float position_mm = status.pulses_per_meter
                            ? (float)status.position_pulses * 1000.0f /
                                  (float)status.pulses_per_meter
                            : 0.0f;
    used += snprintf(text + used, sizeof(text) - (size_t)used,
                     "],\"i2c\":{\"sda_gpio\":%u,\"sda_level\":%u,"
                     "\"scl_gpio\":%u,\"scl_level\":%u,\"direct_mode\":%s,\"mux_address\":%d,"
                     "\"probe_error\":%ld},\"valid_mask\":%u,\"position_mm\":%.3f,"
                     "\"moving\":%s,\"enabled\":%s,\"stopped\":%s,"
                     "\"sensor_test_mode\":%s}",
                     bus_diag.sda_gpio,(unsigned)(bus_diag.line_flags&1U),
                     bus_diag.scl_gpio,(unsigned)((bus_diag.line_flags>>1)&1U),
                     (bus_diag.line_flags&4U)?"true":"false",
                     bus_diag.mux_detected_address==0xff?-1:bus_diag.mux_detected_address,
                     (long)bus_diag.mux_probe_error,valid_mask,position_mm,
                     status.moving ? "true" : "false",
                     status.enabled ? "true" : "false",
                     status.emergency_stop ? "true" : "false",
                     sensor_test_is_enabled() ? "true" : "false");
    httpd_ws_frame_t frame = {
        .type = HTTPD_WS_TYPE_TEXT,
        .payload = (uint8_t *)text,
        .len = (size_t)used,
    };
    if (xSemaphoreTake(send_lock, pdMS_TO_TICKS(20)) != pdTRUE) return;
    if (httpd_ws_send_frame_async(server, fd, &frame) != ESP_OK) websocket_fd = -1;
    xSemaphoreGive(send_lock);
}

static void transmit_task(void *argument)
{
    queued_sample_t message;
    while (true) {
        if (xQueueReceive(sample_queue, &message, portMAX_DELAY) == pdTRUE) {
            transmit_sample(&message);
        }
    }
}

void wifi_web_send_samples(uint32_t uptime_ms,
                           const sensor_sample_t samples[SENSOR_COUNT])
{
    static bool task_started;
    if (!task_started) {
        if (xTaskCreate(transmit_task, "wifi-samples", 6144, NULL, 4, NULL) != pdPASS) return;
        task_started = true;
    }
    queued_sample_t message = {
        .uptime_ms = uptime_ms,
        .sequence = sample_sequence++,
    };
    memcpy(message.samples, samples, sizeof(message.samples));
    if (xQueueSend(sample_queue, &message, 0) != pdTRUE) {
        queued_sample_t discarded;
        (void)xQueueReceive(sample_queue, &discarded, 0);
        (void)xQueueSend(sample_queue, &message, 0);
    }
}
