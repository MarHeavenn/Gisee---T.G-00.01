#include <string.h>

#include "freertos/FreeRTOS.h"
#include "freertos/event_groups.h"
#include "esp_check.h"
#include "esp_event.h"
#include "esp_log.h"
#include "esp_netif.h"
#include "esp_wifi.h"

#include "gisee_net.h"

#define TAG             "gisee_net"
#define CONNECTED_BIT   BIT0

static EventGroupHandle_t s_ev;

static void on_event(void *arg, esp_event_base_t base, int32_t id, void *data)
{
    if (base == WIFI_EVENT && id == WIFI_EVENT_STA_START) {
        esp_wifi_connect();
    } else if (base == WIFI_EVENT && id == WIFI_EVENT_STA_DISCONNECTED) {
        xEventGroupClearBits(s_ev, CONNECTED_BIT);
        ESP_LOGW(TAG, "WiFi desconectado, reintentando...");
        vTaskDelay(pdMS_TO_TICKS(1000));
        esp_wifi_connect();
    } else if (base == IP_EVENT && id == IP_EVENT_STA_GOT_IP) {
        const ip_event_got_ip_t *e = (const ip_event_got_ip_t *)data;
        ESP_LOGI(TAG, "IP obtenida: " IPSTR, IP2STR(&e->ip_info.ip));
        xEventGroupSetBits(s_ev, CONNECTED_BIT);
    }
}

esp_err_t gisee_net_start(const char *ssid, const char *password)
{
    s_ev = xEventGroupCreate();
    ESP_RETURN_ON_FALSE(s_ev, ESP_ERR_NO_MEM, TAG, "event group");

    ESP_RETURN_ON_ERROR(esp_netif_init(), TAG, "netif");
    esp_err_t r = esp_event_loop_create_default();
    if (r != ESP_OK && r != ESP_ERR_INVALID_STATE) {      /* ya existía: ok */
        return r;
    }
    esp_netif_create_default_wifi_sta();

    wifi_init_config_t cfg = WIFI_INIT_CONFIG_DEFAULT();
    ESP_RETURN_ON_ERROR(esp_wifi_init(&cfg), TAG, "wifi_init");

    ESP_RETURN_ON_ERROR(esp_event_handler_register(WIFI_EVENT, ESP_EVENT_ANY_ID, on_event, NULL), TAG, "ev wifi");
    ESP_RETURN_ON_ERROR(esp_event_handler_register(IP_EVENT, IP_EVENT_STA_GOT_IP, on_event, NULL), TAG, "ev ip");

    wifi_config_t wc = {0};
    strlcpy((char *)wc.sta.ssid, ssid, sizeof(wc.sta.ssid));
    strlcpy((char *)wc.sta.password, password, sizeof(wc.sta.password));
    wc.sta.threshold.authmode = password[0] ? WIFI_AUTH_WPA2_PSK : WIFI_AUTH_OPEN;

    ESP_RETURN_ON_ERROR(esp_wifi_set_mode(WIFI_MODE_STA), TAG, "mode");
    ESP_RETURN_ON_ERROR(esp_wifi_set_config(WIFI_IF_STA, &wc), TAG, "config");
    ESP_RETURN_ON_ERROR(esp_wifi_start(), TAG, "start");

    /* Sin ahorro de energía: cuando el WiFi duerme, el audio en streaming
     * llega a golpes y se oye como cortes de varios cientos de ms. */
    esp_err_t ps = esp_wifi_set_ps(WIFI_PS_NONE);
    if (ps == ESP_OK) {
        ESP_LOGI(TAG, "Ahorro de energía WiFi desactivado");
    } else {
        ESP_LOGW(TAG, "No se pudo desactivar el ahorro de energía WiFi: %s",
                 esp_err_to_name(ps));
    }

    ESP_LOGI(TAG, "Conectando a \"%s\"...", ssid);
    return ESP_OK;
}

esp_err_t gisee_net_wait(uint32_t timeout_ms)
{
    EventBits_t b = xEventGroupWaitBits(s_ev, CONNECTED_BIT, pdFALSE, pdTRUE, pdMS_TO_TICKS(timeout_ms));
    return (b & CONNECTED_BIT) ? ESP_OK : ESP_ERR_TIMEOUT;
}

bool gisee_net_is_connected(void)
{
    return s_ev && (xEventGroupGetBits(s_ev) & CONNECTED_BIT);
}
