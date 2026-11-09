/*
 * Gisee en el JC-ESP32P4-M3-DEV: escucha, piensa y habla.
 *
 *   ESP32-P4  --(WiFi)-->  FastAPI (Whisper + Groq + RAG)  --> MP3 (TTS)
 *      ^                        ^                                 |
 *      +--- mic: voz ("hola Gisee") o botón BOOT ----------------+
 *
 * Activación por voz:
 *   - La placa espera callada. Cuando detecta que alguien empezó a hablar,
 *     graba hasta que se hace silencio y manda ese fragmento a /wake.
 *   - Si el servidor reconoce "hola Gisee", Gisee contesta "¿Sí? Te escucho."
 *     y graba la siguiente frase para responderla con /chat/audio.
 *   - Si en la misma frase ya venía el mensaje ("hola Gisee, me siento
 *     triste"), se responde directo sin volver a preguntar.
 *
 * Botón BOOT (GPIO35), se mantiene como respaldo:
 *   - mantener pulsado  : graba tu voz por el micrófono (hasta 10 s)
 *   - al soltar         : WAV -> /stt (Whisper), texto -> /chat/audio
 *   - toque corto en    : corta la reproducción en curso
 *     pleno de playback
 */

#include <string.h>
#include <stdint.h>
#include <stdlib.h>
#include <math.h>

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
#define REC_MAX_MS          10000      /* grabación máxima con el botón */
#define REC_BUF_FRAMES_MAX  (REC_RATE * REC_MAX_MS / 1000)
#define CHUNK               1024
#define WAV_HDR             44

/* --- Detección de voz para "hola Gisee" --------------------------------- */
#define VAD_FRAME_MS        20
#define VAD_FRAME_BYTES     (REC_RATE * VAD_FRAME_MS / 1000 * 2)   /* 640 */
#define VAD_PREROLL_MS      400
#define VAD_PREROLL_FRAMES  (VAD_PREROLL_MS / VAD_FRAME_MS)
#define VAD_SILENCIO_MS     700        /* silencio que cierra la frase */
#define VAD_SILENCIO_FRAMES (VAD_SILENCIO_MS / VAD_FRAME_MS)
#define VAD_MIN_MS          350        /* menos que esto: ruido, se descarta */
#define VAD_MAX_MS          6000       /* frase máxima que se envía a /wake */
#define VAD_PCM_BYTES       (REC_RATE * VAD_MAX_MS / 1000 * 2)
#define VAD_ESPERA_MAX_MS   15000      /* sin voz: revisar red/botón y seguir */

#ifndef CONFIG_GISEE_WAKE_RMS
#define CONFIG_GISEE_WAKE_RMS 500
#endif

static TaskHandle_t s_gisee_task;
static volatile bool s_btn_grabar;     /* el botón pide una grabación manual */

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

/* Energía de un frame de 20 ms, para distinguir voz de silencio. */
static int frame_rms(const uint8_t *frame, int bytes)
{
    const int16_t *s = (const int16_t *)(const void *)frame;
    int n = bytes / 2;
    if (n <= 0) {
        return 0;
    }
    int64_t suma = 0;
    for (int i = 0; i < n; i++) {
        int32_t v = s[i];
        suma += (int64_t)v * v;
    }
    return (int)sqrt((double)(suma / n));
}

/*
 * Graba automáticamente una frase: espera a que haya voz, la captura y
 * termina cuando la persona se calla. Devuelve los bytes PCM grabados
 * (0 si no hubo voz, si se canceló o si el botón tomó el control).
 *
 * El ruido de fondo se mide solo y el umbral sube con él, para que un
 * ventilador o el aire acondicionado no disparen la escucha en bucle.
 */
static size_t grabar_voz(uint8_t *pcm, size_t pcm_cap, volatile bool *cancelar)
{
    const size_t pre_bytes = (size_t)VAD_PREROLL_FRAMES * VAD_FRAME_BYTES;
    uint8_t *pre = heap_caps_malloc(pre_bytes, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
    if (pre == NULL) {
        ESP_LOGE(TAG, "Sin PSRAM para el pre-roll de voz");
        return 0;
    }

    uint8_t frame[VAD_FRAME_BYTES];
    size_t total = 0;
    int ruido = CONFIG_GISEE_WAKE_RMS / 4;
    int umbral = CONFIG_GISEE_WAKE_RMS;
    int silencio = 0;
    int arranque = 0;
    int esperando = 0;
    int pre_count = 0;
    int pre_idx = 0;
    bool grabando = false;

    if (ruido < 100) {
        ruido = 100;
    }

    while (true) {
        if (cancelar != NULL && *cancelar) {
            break;
        }

        int n = gisee_audio_mic_read_mono16(frame, VAD_FRAME_BYTES);
        if (n <= 0) {
            ESP_LOGW(TAG, "El micrófono dejó de entregar audio");
            break;
        }

        int rms = frame_rms(frame, n);

        /* Guardar siempre el último medio segundo: cuando empiece la voz,
         * el "hola" no se pierde por haber llegado antes del disparo. */
        memcpy(pre + (size_t)pre_idx * VAD_FRAME_BYTES, frame, VAD_FRAME_BYTES);
        pre_idx = (pre_idx + 1) % VAD_PREROLL_FRAMES;
        if (pre_count < VAD_PREROLL_FRAMES) {
            pre_count++;
        }

        if (!grabando) {
            if (++esperando > (VAD_ESPERA_MAX_MS / VAD_FRAME_MS)) {
                break;                                  /* sin voz: vuelve al loop */
            }
            if (rms < umbral) {
                ruido = (ruido * 31 + rms) / 32;
                int nuevo = ruido * 3;
                umbral = nuevo > CONFIG_GISEE_WAKE_RMS ? nuevo : CONFIG_GISEE_WAKE_RMS;
                arranque = 0;
                continue;
            }
            if (++arranque < 3) {
                continue;                               /* 60 ms seguidos de voz */
            }

            int inicio = (pre_idx - pre_count + VAD_PREROLL_FRAMES) % VAD_PREROLL_FRAMES;
            for (int i = 0; i < pre_count && total < pcm_cap; i++) {
                int idx = (inicio + i) % VAD_PREROLL_FRAMES;
                size_t copiar = VAD_FRAME_BYTES;
                if (total + copiar > pcm_cap) {
                    copiar = pcm_cap - total;
                }
                memcpy(pcm + total, pre + (size_t)idx * VAD_FRAME_BYTES, copiar);
                total += copiar;
            }
            grabando = true;
            silencio = 0;
            ESP_LOGI(TAG, "Voz detectada: grabando la frase...");
            continue;                                   /* el frame ya está en el pre-roll */
        }

        if (total + (size_t)n <= pcm_cap) {
            memcpy(pcm + total, frame, (size_t)n);
            total += (size_t)n;
        }

        if (rms >= umbral) {
            silencio = 0;
        } else if (++silencio >= VAD_SILENCIO_FRAMES) {
            break;                                      /* se calló: frase completa */
        }

        if (total >= pcm_cap) {
            break;
        }
    }

    gisee_audio_mic_close();
    heap_caps_free(pre);

    size_t minimo = (size_t)(REC_RATE * VAD_MIN_MS / 1000 * 2);
    if (total < minimo) {
        return 0;
    }
    return total;
}

/* Push-to-talk: graba mientras el botón esté pulsado y reproduce la respuesta. */
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

/* Escucha una frase y pregunta al servidor si dijo "hola Gisee".
 * Si la dijo, deja en `resto` lo que vino después del nombre. */
static bool esperar_activacion(uint8_t *pcm, size_t pcm_cap,
                               char *resto, size_t resto_cap)
{
    size_t have = grabar_voz(pcm + WAV_HDR, pcm_cap, &s_btn_grabar);
    if (have == 0) {
        return false;
    }

    wav_header(pcm, REC_RATE, 16, 1, (uint32_t)have);
    char texto[512];
    bool activado = false;
    esp_err_t r = gisee_wake(pcm, have + WAV_HDR, texto, sizeof(texto),
                             resto, resto_cap, &activado);
    if (r != ESP_OK || !activado) {
        return false;
    }
    ESP_LOGI(TAG, "Dijiste: \"%s\" -> activado", texto);
    return true;
}

/* Bucle principal: espera "hola Gisee", escucha la pregunta y responde. */
static void conversacion(void)
{
    uint8_t *pcm = heap_caps_malloc(VAD_PCM_BYTES + WAV_HDR,
                                    MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
    if (pcm == NULL) {
        ESP_LOGE(TAG, "Sin PSRAM para escuchar");
        vTaskDelay(pdMS_TO_TICKS(1000));
        return;
    }

    while (true) {
        if (s_btn_grabar) {                       /* el botón tiene prioridad */
            s_btn_grabar = false;
            record_and_reply();
            continue;
        }
        if (!gisee_net_is_connected()) {
            ESP_LOGW(TAG, "Sin red: reintentando...");
            vTaskDelay(pdMS_TO_TICKS(1000));
            continue;
        }

        char resto[512];
        resto[0] = '\0';
        if (!esperar_activacion(pcm, VAD_PCM_BYTES, resto, sizeof(resto))) {
            continue;                             /* no dijo el nombre: a seguir oyendo */
        }

        if (resto[0] != '\0') {
            /* Ya venía el mensaje en la misma frase: responder directo. */
            ESP_LOGI(TAG, "Mensaje en el saludo: \"%s\"", resto);
            if (gisee_chat(resto) != ESP_OK) {
                gisee_audio_beep(300, 300);
            }
        } else {
            gisee_say("¿Sí? Te escucho.");
            ESP_LOGI(TAG, "Esperando la pregunta...");

            size_t have = grabar_voz(pcm + WAV_HDR, VAD_PCM_BYTES, &s_btn_grabar);
            if (have > 0) {
                wav_header(pcm, REC_RATE, 16, 1, (uint32_t)have);
                char texto[512];
                if (gisee_stt(pcm, have + WAV_HDR, texto, sizeof(texto)) == ESP_OK &&
                    texto[0] != '\0') {
                    ESP_LOGI(TAG, "Pregunta: \"%s\"", texto);
                    if (gisee_chat(texto) != ESP_OK) {
                        gisee_audio_beep(300, 300);
                    }
                }
            }
        }

        /* Pausa para no capturar el eco de la propia bocina. */
        vTaskDelay(pdMS_TO_TICKS(400));
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

    conversacion();
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
                        s_btn_grabar = true;
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

    xTaskCreate(gisee_task, "gisee", 16384, NULL, 5, &s_gisee_task);
    xTaskCreate(button_task, "gisee_btn", 2048, NULL, 3, NULL);
}
