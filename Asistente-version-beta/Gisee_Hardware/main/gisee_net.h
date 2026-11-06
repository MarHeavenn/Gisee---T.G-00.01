#pragma once

#include <stdbool.h>
#include <stdint.h>
#include "esp_err.h"

/* Arranca el WiFi en modo estación y se reconecta solo si se cae. */
esp_err_t gisee_net_start(const char *ssid, const char *password);

/* Espera a tener IP. ESP_ERR_TIMEOUT si no llega. */
esp_err_t gisee_net_wait(uint32_t timeout_ms);

bool gisee_net_is_connected(void);
