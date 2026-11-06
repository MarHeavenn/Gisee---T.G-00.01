/*
 * Gisee en el JC-ESP32P4-M3-DEV: escucha, piensa y habla.
 *
 *   ESP32-P4  --(WiFi)-->  FastAPI (Whisper + Groq + RAG)  --> MP3 (TTS)
 *      ^                        ^                                 |
 *      +--- mic SIEMPRE en "push-to-talk" (WAV en streaming) ------+
 *
 * Botón BOOT (GPIO35):
 *   - mantener pulsado  : graba tu voz por el micrófono (hasta 10 s)
 *   - al soltar         : WAV -> /stt (Whisper), texto -> /chat/audio
 *                         (Groq + RAG) -> respuesta MP3 por la bocina
 *   - toque corto en    : corta la reproducción en curso
 *     pleno de playback
 */

#include <string.h>
#include <stdint.h>
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "driver/gpio.h"
#include "esp_heap_caps.h"
#include "esp_log.h"
#include "nvs_flash.h"
#include "sdkconfig.h"

#include "gisee_audio.h"
#include "gisee_net.h"

#define TAG                 "gisee"
#define BUTTON_GPIO         GPIO_NUM_35
#define REC_RATE            16000
#define REC_MAX_MS          10000      /* grabación máxima */
#define REC_BUF_FRAMES_MAX  (REC_RATE * REC_MAX_MS / 1000)
#define CHUNK               1024
#define WAV_HDR             44

static TaskHandle_t s_gisee_task;

static bool btn_down(void)
{
    return gpio_get_level(BUTTON_GPIO) == 0;
}

static void put_u32le(uint8_t *p, uint32_t v)
{
    p[0] = (uint8_t)(v & 0xff);
    p[1] = (uint8_t)((v >> 8) & 0xff);
    p[2] = (uint8_t)((v >> 16) & 0xff);
    p[3] = (uint8_t)((v >> 24) & 0xff);
}

static void put_u16le(uint8_t *p, uint16_t v)
{
    p[0] = (uint8_t)(v & 0xff);
    p[1] = (uint8_t)((v >> 8) & 0xff);
}

/* Envuelve `data_len` muestras PCM (rate/bits/ch) en un WAV de 44 bytes prefijo. */
static void wav_header(uint8_t *h, uint32_t rate, uint16_t bits, uint16_t ch, uint32_t data_len)
{
    memcpy(h, "RIFF", 4);
    put_u32le(h + 4, 36 + data_len);
    memcpy(h + 8, "WAVE", 4);
    memcpy(h + 12, "fmt ", 4);
    put_u32le(h + 16, 16);
    put_u16le(h + 20, 1);                     /* PCM */
    put_u16le(h + 22, ch);
    put_u32le(h + 24, rate);
    put_u32le(h + 28, rate * ch * bits / 8);
    put_u16le(h + 32, ch * bits / 8);
    put_u16le(h + 34, bits);
    memcpy(h + 36, "data", 4);
    put_u32le(h + 40, data_len);
}

/* Push-to-talk: graba y reproduce la respuesta de la IA. */
static void record_and_reply(void)
{
    const size_t pcm_cap = (size_t)REC_BUF_FRAMES_MAX * 2;
    /* +44 bytes para la cabecera WAV (se escribe al final, cuando se sabe 'have'.) */
    uint8_t *wav = heap_caps_malloc(pcm_cap + WAV_HDR, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
    if (wav == NULL) {
        ESP_LOGE(TAG, "Sin PSRAM para grabar");
        gisee_audio_beep(300, 300);
        return;
    }

    gisee_audio_beep(1200, 120);              /* aviso: empiezo a escuchar */

    size_t have = 0;
    while (have < pcm_cap && btn_down()) {
        /* No pedir siempre CHUNK: al final del buffer solo queda el hueco
         * restante y pedir más escribiría fuera del malloc. */
        size_t resto = pcm_cap - have;
        int want = resto < CHUNK ? (int)resto : CHUNK;
        int n = gisee_audio_mic_read_mono16(wav + WAV_HDR + have, want);
        if (n <= 0) {
            ESP_LOGW(TAG, "El micrófono dejó de entregar audio");
            break;
        }
        have += (size_t)n;
    }
    gisee_audio_mic_close();

    if (have < REC_RATE / 4) {                /* menos de 1/4 s: descarta */
        ESP_LOGW(TAG, "Grabación muy corta, no llegó a capturar nada útil");
        heap_caps_free(wav);
        gisee_audio_beep(300, 200);
        return;
    }
    wav_header(wav, REC_RATE, 16, 1, (uint32_t)have);
    gisee_audio_beep(800, 90);                /* aviso: ya te escuché, pienso */
    ESP_LOGI(TAG, "Grabados %u bytes de PCM (%.1f s), enviando a /stt...",
             (unsigned)have, (float)have / (REC_RATE * 2));

    char texto[512];
    esp_err_t r = gisee_stt(wav, have + WAV_HDR, texto, sizeof(texto));
    heap_caps_free(wav);                      /* ya no hace falta */
    if (r != ESP_OK) {
        ESP_LOGE(TAG, "No se pudo transcribir");
        gisee_audio_beep(300, 300);
        return;
    }
    ESP_LOGI(TAG, "Dijiste: \"%s\"", texto);
    if (gisee_chat(texto) != ESP_OK) {         /* respuesta por la bocina */
        ESP_LOGE(TAG, "El servidor no pudo responder");
        gisee_audio_beep(300, 300);
    }
}

static void gisee_task(void *arg)
{
    (void)arg;

    if (gisee_net_wait(30000) != ESP_OK) {
        ESP_LOGE(TAG, "Sin WiFi: revisa SSID/contraseña y el WiFi hospedado del C6");
        gisee_audio_beep(300, 500);
    } else {
        gisee_say("Hola, soy Gisee. Ya estoy listo para acompañarte. "
                  "Cuéntame, ¿cómo te sientes hoy?");
    }

    while (true) {
        ulTaskNotifyTake(pdTRUE, portMAX_DELAY);          /* espera pulsación */
        if (!gisee_net_is_connected()) {
            ESP_LOGW(TAG, "Sin red");
            gisee_audio_beep(300, 300);
            continue;
        }
        record_and_reply();
    }
}

static void button_task(void *arg)
{
    (void)arg;

    int last = gpio_get_level(BUTTON_GPIO);
    while (true) {
        int level = gpio_get_level(BUTTON_GPIO);
        if (level != last) {
            vTaskDelay(pdMS_TO_TICKS(30));                 /* antirrebote */
            level = gpio_get_level(BUTTON_GPIO);
            if (level != last) {
                if (level == 0) {
                    if (gisee_audio_is_playing()) {
                        gisee_audio_stop();
                    } else {
                        xTaskNotifyGive(s_gisee_task);
                    }
                }
                last = level;
            }
        }
        vTaskDelay(pdMS_TO_TICKS(20));
    }
}

void app_main(void)
{
    esp_err_t r = nvs_flash_init();
    if (r == ESP_ERR_NVS_NO_FREE_PAGES || r == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        ESP_ERROR_CHECK(nvs_flash_erase());
        r = nvs_flash_init();
    }
    ESP_ERROR_CHECK(r);

    ESP_ERROR_CHECK(gisee_audio_init());
    ESP_ERROR_CHECK(gisee_audio_mic_init());

    /* El códec comparte un solo I2S para altavoz y micro. Al reconfigurarlo,
     * el driver intenta desactivar canales que ya estaban apagados y escupe
     * "i2s_channel_disable: the channel has not been enabled yet" a nivel
     * ERROR en cada cambio de formato. Es ruido interno, no un fallo real,
     * así que lo silenciamos para que el log de la placa sea legible. */
    esp_log_level_set("i2s_common", ESP_LOG_NONE);

    /* Pitido corto de arranque. La autoprueba completa de 3 fases
     * (gisee_audio_selftest) ya sirvió para validar el PA y el pinout; se
     * puede volver a llamar aquí si hace falta diagnosticar la bocina. */
    gisee_audio_beep(1000, 90);

    gpio_config_t io = {
        .pin_bit_mask = 1ULL << BUTTON_GPIO,
        .mode = GPIO_MODE_INPUT,
        .pull_up_en = GPIO_PULLUP_ENABLE,
        .intr_type = GPIO_INTR_DISABLE,
    };
    gpio_config(&io);

    ESP_ERROR_CHECK(gisee_net_start(CONFIG_GISEE_WIFI_SSID, CONFIG_GISEE_WIFI_PASSWORD));

    xTaskCreate(gisee_task, "gisee", 12288, NULL, 5, &s_gisee_task);
    xTaskCreate(button_task, "gisee_btn", 2048, NULL, 3, NULL);
}
