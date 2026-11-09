/*
 * Audio de Gisee: reproduce por la bocina el MP3 que entrega el servidor
 * (FastAPI + Groq + RAG + TTS) en streaming, sin guardarlo en la SD.
 *
 * Hardware real (JC-ESP32P4-M3-DEV, esquemático oficial de GUITION):
 *   ES8311 (I2S0) -> NS4150 -> bocina.
 *   - PA_CTRL (enable del NS4150) = GPIO11
 *   - MCLK = GPIO13, SCLK = GPIO12, LRCK = GPIO10
 *   - DSDIN del ES8311 (datos ESP32 -> codec) = GPIO9
 *   - ASDOUT del ES8311 (datos codec -> ESP32) = GPIO48
 *
 * La BSP esp32_p4_function_ev_board asume el pinout de otra placa
 * (DSIN = GPIO11 y PA = GPIO53), así que aquí se le pasa una configuración
 * I2S propia antes de crear el dispositivo del codec.
 */

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <math.h>

#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/semphr.h"
#include "driver/gpio.h"
#include "driver/i2s_std.h"
#include "esp_log.h"
#include "esp_http_client.h"
#include "cJSON.h"
#include "sdkconfig.h"

#include "bsp/esp-bsp.h"
#include "esp_codec_dev.h"

#include "esp_audio_types.h"
#include "esp_audio_dec_default.h"
#include "esp_audio_simple_dec.h"
#include "esp_audio_simple_dec_default.h"

#include "gisee_audio.h"

#define TAG             "gisee_audio"

#define READ_BUF_SIZE   4096      /* bytes MP3 leídos de la red cada vez */
#define OUT_BUF_SIZE    8192      /* buffer inicial de PCM decodificado */
#define MIC_RAW_FRAMES  256       /* lectura del mic en trozos */

/* ASDOUT del ES8311 -> DIN del I2S del ESP32. En la BSP es GPIO11, pero en
 * esta placa GPIO11 es PA_CTRL y el dato del codec entra por GPIO48. */
#define GISEE_I2S_DIN   GPIO_NUM_48

static esp_codec_dev_handle_t s_spk;
static esp_codec_dev_handle_t s_mic;
static SemaphoreHandle_t      s_lock;          /* un solo audio a la vez */
static volatile bool          s_stop;
static volatile bool          s_playing;
static bool                   s_mic_open;
static bool                   s_mic_stereo;

/* ------------------------------------------------------------------------- */
/* Amplificador NS4150 (igual que el reproductor MP3 de referencia)          */
/* ------------------------------------------------------------------------- */

static void pa_enable(void)
{
#if CONFIG_GISEE_PA_GPIO >= 0
    gpio_config_t io = {
        .pin_bit_mask = 1ULL << CONFIG_GISEE_PA_GPIO,
        .mode = GPIO_MODE_INPUT_OUTPUT,
        .pull_up_en = GPIO_PULLUP_DISABLE,
        .pull_down_en = GPIO_PULLDOWN_DISABLE,
        .intr_type = GPIO_INTR_DISABLE,
    };
    gpio_config(&io);
    gpio_set_level(CONFIG_GISEE_PA_GPIO, 1);
#endif
}

/* ------------------------------------------------------------------------- */

/* Inicializa el I2S con el pinout REAL de esta placa. Hay que hacerlo antes
 * de bsp_audio_codec_speaker_init(): esa función solo llama a
 * bsp_audio_init(NULL) si el I2S todavía no existe, y con NULL usaría los
 * pines equivocados de la BSP (DSIN=GPIO11, que aquí es el PA). */
static esp_err_t audio_i2s_init(void)
{
    esp_err_t err = bsp_i2c_init();
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "No se pudo inicializar el I2C del codec: %s",
                 esp_err_to_name(err));
        return err;
    }

    i2s_std_config_t cfg = {
        .clk_cfg  = I2S_STD_CLK_DEFAULT_CONFIG(22050),
        .slot_cfg = I2S_STD_PHILIP_SLOT_DEFAULT_CONFIG(I2S_DATA_BIT_WIDTH_16BIT,
                                                       I2S_SLOT_MODE_MONO),
        .gpio_cfg = {
            .mclk = BSP_I2S_MCLK,    /* GPIO13 */
            .bclk = BSP_I2S_SCLK,    /* GPIO12 */
            .ws   = BSP_I2S_LCLK,    /* GPIO10 */
            .dout = BSP_I2S_DOUT,    /* GPIO9  -> DSDIN del ES8311 */
            .din  = GISEE_I2S_DIN,   /* GPIO48 <- ASDOUT del ES8311 */
            .invert_flags = {
                .mclk_inv = false,
                .bclk_inv = false,
                .ws_inv   = false,
            },
        },
    };

    err = bsp_audio_init(&cfg);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "No se pudo inicializar el I2S: %s", esp_err_to_name(err));
    }
    return err;
}

esp_err_t gisee_audio_init(void)
{
    s_lock = xSemaphoreCreateMutex();
    if (s_lock == NULL) {
        return ESP_ERR_NO_MEM;
    }

    esp_err_t err = audio_i2s_init();
    if (err != ESP_OK) {
        return err;
    }

    s_spk = bsp_audio_codec_speaker_init();
    if (s_spk == NULL) {
        ESP_LOGE(TAG, "No se pudo inicializar el codec de audio");
        return ESP_FAIL;
    }
    esp_codec_dev_set_out_vol(s_spk, CONFIG_GISEE_VOLUME);
    pa_enable();

    if (esp_audio_dec_register_default() != ESP_AUDIO_ERR_OK) {
        ESP_LOGW(TAG, "No se pudieron registrar los decodificadores por defecto");
    }
    if (esp_audio_simple_dec_register_default() != ESP_AUDIO_ERR_OK) {
        ESP_LOGW(TAG, "No se pudieron registrar los decodificadores simples");
    }
    ESP_LOGI(TAG, "Audio listo: volumen %d, PA GPIO%d, DIN GPIO%d",
             CONFIG_GISEE_VOLUME, CONFIG_GISEE_PA_GPIO, (int)GISEE_I2S_DIN);
    return ESP_OK;
}

void gisee_audio_set_volume(int volume)
{
    if (s_spk) {
        esp_codec_dev_set_out_vol(s_spk, volume);
    }
}

void gisee_audio_stop(void)       { s_stop = true; }
bool gisee_audio_is_playing(void) { return s_playing; }

/* ------------------------------------------------------------------------- */
/* Captura del micrófono (ES7210)                                            */
/* ------------------------------------------------------------------------- */

esp_err_t gisee_audio_mic_init(void)
{
    s_mic = bsp_audio_codec_microphone_init();
    if (s_mic == NULL) {
        ESP_LOGE(TAG, "No se pudo inicializar el micrófono");
        return ESP_FAIL;
    }
    ESP_LOGI(TAG, "Micrófono listo");
    return ESP_OK;
}

int gisee_audio_mic_read_mono16(uint8_t *dst, int bytes)
{
    if (s_mic == NULL || dst == NULL) {
        return -1;
    }
    if (!s_mic_open) {
        esp_codec_dev_sample_info_t fs = { .sample_rate = 16000, .bits_per_sample = 16, .channel = 1 };
        if (esp_codec_dev_open(s_mic, &fs) != ESP_CODEC_DEV_OK) {
            fs.channel = 2;
            if (esp_codec_dev_open(s_mic, &fs) != ESP_CODEC_DEV_OK) {
                ESP_LOGE(TAG, "No se pudo abrir el micrófono");
                return -1;
            }
            s_mic_stereo = true;
        } else {
            s_mic_stereo = false;
        }
        s_mic_open = true;
        ESP_LOGI(TAG, "Mic grabando: 16 kHz, 16 bits, %d canal(es)", s_mic_stereo ? 2 : 1);
    }

    if (!s_mic_stereo) {
        /* OJO: esp_codec_dev_read() NO devuelve bytes leídos, devuelve un
         * código de estado (ESP_CODEC_DEV_OK == 0). Interpretar el 0 como
         * "no hay audio" hacía que la grabación se cortara al instante. */
        int ret = esp_codec_dev_read(s_mic, dst, bytes);
        return (ret == ESP_CODEC_DEV_OK) ? bytes : -1;
    }

    /* Estéreo -> mono (promedio de canales). El codec entrega frames largos de
     * bytes: en lugar de adivinar el bloque, leemos en trozos del doble. */
    int16_t raw[MIC_RAW_FRAMES * 2];
    int want = bytes / 2;                     /* muestras PCM pedidas */
    int done = 0;
    while (done < want) {
        int frames = want - done > MIC_RAW_FRAMES ? MIC_RAW_FRAMES : want - done;
        int ret = esp_codec_dev_read(s_mic, raw, frames * 2 * sizeof(int16_t));
        if (ret != ESP_CODEC_DEV_OK) {
            return done > 0 ? done * 2 : -1;
        }
        int16_t *s = (int16_t *)(void *)raw;
        int16_t *d = (int16_t *)(dst + done * 2);
        for (int i = 0; i < frames; i++) {     /* frames = pares L,R */
            d[i] = (int16_t)(((int32_t)s[2 * i] + (int32_t)s[2 * i + 1]) / 2);
        }
        done += frames;
    }
    return done * 2;
}

void gisee_audio_mic_close(void)
{
    if (s_mic != NULL && s_mic_open) {
        esp_codec_dev_close(s_mic);
        s_mic_open = false;
        s_mic_stereo = false;
    }
}

/* ------------------------------------------------------------------------- */
/* STT: envía un WAV al servidor y devuelve el texto transcrito              */
/* ------------------------------------------------------------------------- */

/* Envía un WAV a `ruta` y deja la respuesta JSON cruda en `resp`. */
static esp_err_t enviar_wav(const char *ruta, const uint8_t *wav, size_t wav_len,
                            char *resp, size_t resp_cap)
{
    if (wav == NULL || resp == NULL || resp_cap == 0) {
        return ESP_ERR_INVALID_ARG;
    }
    resp[0] = '\0';

    char url[192];
    snprintf(url, sizeof(url), "%s%s", CONFIG_GISEE_SERVER_URL, ruta);

    esp_http_client_config_t cfg = {
        .url = url,
        .method = HTTP_METHOD_POST,
        .timeout_ms = 30000,
        .buffer_size = 2048,
    };
    esp_http_client_handle_t http = esp_http_client_init(&cfg);
    if (http == NULL) {
        return ESP_FAIL;
    }
    esp_http_client_set_header(http, "Content-Type", "audio/wav");

    esp_err_t ret = esp_http_client_open(http, (int)wav_len);
    if (ret != ESP_OK) {
        ESP_LOGE(TAG, "No se pudo conectar a %s: %s", ruta, esp_err_to_name(ret));
        esp_http_client_cleanup(http);
        return ret;
    }

    /* esp_http_client_write puede aceptar menos bytes de los pedidos: hay que
     * repetir hasta enviar el WAV completo o la grabación llega truncada. */
    size_t enviado = 0;
    while (enviado < wav_len) {
        int w = esp_http_client_write(http, (const char *)wav + enviado,
                                      (int)(wav_len - enviado));
        if (w <= 0) {
            ESP_LOGE(TAG, "Se cortó el envío del WAV (%u/%u bytes)",
                     (unsigned)enviado, (unsigned)wav_len);
            esp_http_client_cleanup(http);
            return ESP_FAIL;
        }
        enviado += (size_t)w;
    }

    if (esp_http_client_fetch_headers(http) < 0) {
        ESP_LOGE(TAG, "Sin cabeceras de %s", ruta);
        esp_http_client_cleanup(http);
        return ESP_FAIL;
    }
    if (esp_http_client_get_status_code(http) != 200) {
        ESP_LOGE(TAG, "%s respondió HTTP %d", ruta, esp_http_client_get_status_code(http));
        esp_http_client_cleanup(http);
        return ESP_FAIL;
    }

    int total = 0, n;
    while (total < (int)resp_cap - 1 &&
           (n = esp_http_client_read(http, resp + total, (int)resp_cap - 1 - total)) > 0) {
        total += n;
    }
    resp[total] = '\0';
    esp_http_client_cleanup(http);
    return (total > 0) ? ESP_OK : ESP_FAIL;
}

esp_err_t gisee_stt(const uint8_t *wav, size_t wav_len, char *texto, size_t texto_cap)
{
    if (texto == NULL || texto_cap == 0) {
        return ESP_ERR_INVALID_ARG;
    }
    texto[0] = '\0';

    char *resp = malloc(1536);
    if (resp == NULL) {
        return ESP_ERR_NO_MEM;
    }
    esp_err_t ret = enviar_wav("/stt", wav, wav_len, resp, 1536);
    if (ret != ESP_OK) {
        free(resp);
        return ret;
    }

    cJSON *root = cJSON_Parse(resp);
    if (root == NULL) {
        ESP_LOGE(TAG, "Respuesta de /stt no es JSON: %.80s", resp);
        free(resp);
        return ESP_FAIL;
    }
    const cJSON *t = cJSON_GetObjectItem(root, "texto");
    if (!cJSON_IsString(t) || t->valuestring[0] == '\0') {
        t = cJSON_GetObjectItem(root, "text");
    }
    if (cJSON_IsString(t) && t->valuestring[0] != '\0') {
        strlcpy(texto, t->valuestring, texto_cap);
    } else {
        ret = ESP_FAIL;
    }
    cJSON_Delete(root);
    free(resp);
    return ret;
}

esp_err_t gisee_wake(const uint8_t *wav, size_t wav_len,
                     char *texto, size_t texto_cap,
                     char *resto, size_t resto_cap,
                     bool *activado)
{
    if (texto == NULL || resto == NULL || activado == NULL) {
        return ESP_ERR_INVALID_ARG;
    }
    texto[0] = '\0';
    resto[0] = '\0';
    *activado = false;

    char *resp = malloc(2048);
    if (resp == NULL) {
        return ESP_ERR_NO_MEM;
    }
    esp_err_t ret = enviar_wav("/wake", wav, wav_len, resp, 2048);
    if (ret != ESP_OK) {
        free(resp);
        return ret;
    }

    cJSON *root = cJSON_Parse(resp);
    if (root == NULL) {
        ESP_LOGE(TAG, "Respuesta de /wake no es JSON: %.80s", resp);
        free(resp);
        return ESP_FAIL;
    }
    const cJSON *w = cJSON_GetObjectItem(root, "wake");
    *activado = cJSON_IsTrue(w);
    const cJSON *t = cJSON_GetObjectItem(root, "texto");
    if (cJSON_IsString(t)) {
        strlcpy(texto, t->valuestring, texto_cap);
    }
    const cJSON *r = cJSON_GetObjectItem(root, "resto");
    if (cJSON_IsString(r)) {
        strlcpy(resto, r->valuestring, resto_cap);
    }
    cJSON_Delete(root);
    free(resp);
    return ESP_OK;
}


/* ------------------------------------------------------------------------- */
/* Pitido de prueba                                                          */
/* ------------------------------------------------------------------------- */

esp_err_t gisee_audio_beep(int freq_hz, int ms)
{
    if (s_spk == NULL) {
        return ESP_ERR_INVALID_STATE;
    }
    xSemaphoreTake(s_lock, portMAX_DELAY);

    const int rate = 16000;
    esp_codec_dev_sample_info_t fs = { .sample_rate = rate, .bits_per_sample = 16, .channel = 2 };
    if (esp_codec_dev_open(s_spk, &fs) != ESP_CODEC_DEV_OK) {
        ESP_LOGE(TAG, "No se pudo abrir el codec para el pitido");
        xSemaphoreGive(s_lock);
        return ESP_FAIL;
    }
    esp_codec_dev_set_out_vol(s_spk, CONFIG_GISEE_VOLUME);
    pa_enable();

    const int total = rate * ms / 1000;
    const int ramp = rate / 200;                      /* 5 ms: evita clics */
    int16_t buf[256 * 2];
    float phase = 0.0f;
    const float step = 2.0f * (float)M_PI * freq_hz / rate;

    for (int done = 0; done < total; ) {
        int n = total - done > 256 ? 256 : total - done;
        for (int i = 0; i < n; i++) {
            int pos = done + i;
            float env = 1.0f;
            if (pos < ramp)               env = (float)pos / ramp;
            else if (pos > total - ramp)  env = (float)(total - pos) / ramp;
            int16_t s = (int16_t)(sinf(phase) * env * 0.4f * 32767.0f);
            phase += step;
            if (phase > 2.0f * (float)M_PI) phase -= 2.0f * (float)M_PI;
            buf[2 * i] = s;
            buf[2 * i + 1] = s;
        }
        if (esp_codec_dev_write(s_spk, buf, n * 2 * sizeof(int16_t)) != ESP_CODEC_DEV_OK) {
            ESP_LOGE(TAG, "El codec rechazó el pitido de %d Hz", freq_hz);
            break;
        }
        done += n;
    }

    esp_codec_dev_close(s_spk);
    xSemaphoreGive(s_lock);
    return ESP_OK;
}

/* ------------------------------------------------------------------------- */
/* Prueba de la cadena de salida (bocina)                                    */
/* ------------------------------------------------------------------------- */

static void leer_registro(const char *nombre, int reg)
{
    int valor = -1;
    if (esp_codec_dev_read_reg(s_spk, reg, &valor) == ESP_CODEC_DEV_OK) {
        ESP_LOGI(TAG, "ES8311 %s [0x%02X] = 0x%02X", nombre, reg, valor);
    } else {
        ESP_LOGW(TAG, "ES8311 %s [0x%02X] no se pudo leer", nombre, reg);
    }
}

/* Toca un tono sin abrir/cerrar el codec: se usa dentro de la autoprueba,
 * donde el dispositivo ya está abierto y el lock tomado. */
static esp_err_t tono_simple(int freq_hz, int ms, float amplitud)
{
    const int rate = 16000;
    const int total = rate * ms / 1000;
    const int ramp = rate / 200;
    int16_t buf[256 * 2];
    float phase = 0.0f;
    const float step = 2.0f * (float)M_PI * freq_hz / rate;

    for (int done = 0; done < total; ) {
        int n = total - done > 256 ? 256 : total - done;
        for (int i = 0; i < n; i++) {
            int pos = done + i;
            float env = 1.0f;
            if (pos < ramp)               env = (float)pos / ramp;
            else if (pos > total - ramp)  env = (float)(total - pos) / ramp;
            int16_t s = (int16_t)(sinf(phase) * env * amplitud * 32767.0f);
            phase += step;
            if (phase > 2.0f * (float)M_PI) phase -= 2.0f * (float)M_PI;
            buf[2 * i] = s;
            buf[2 * i + 1] = s;
        }
        if (esp_codec_dev_write(s_spk, buf, n * 2 * sizeof(int16_t)) != ESP_CODEC_DEV_OK) {
            return ESP_FAIL;
        }
        done += n;
    }
    return ESP_OK;
}

esp_err_t gisee_audio_selftest(void)
{
    if (s_spk == NULL) {
        return ESP_ERR_INVALID_STATE;
    }
    xSemaphoreTake(s_lock, portMAX_DELAY);

    esp_codec_dev_sample_info_t fs = { .sample_rate = 16000, .bits_per_sample = 16, .channel = 2 };
    if (esp_codec_dev_open(s_spk, &fs) != ESP_CODEC_DEV_OK) {
        ESP_LOGE(TAG, "No se pudo abrir el codec para la autoprueba");
        xSemaphoreGive(s_lock);
        return ESP_FAIL;
    }
    esp_codec_dev_set_out_vol(s_spk, 100);

    /* Si el DAC estuviera en mute o el volumen a cero, aquí se vería. */
    leer_registro("DAC control", 0x31);
    leer_registro("DAC volumen", 0x32);
    leer_registro("DAC rampa", 0x37);
    leer_registro("system", 0x0D);
    leer_registro("system", 0x12);

    const int pa = CONFIG_GISEE_PA_GPIO;
    pa_enable();

    ESP_LOGW(TAG, "AUTOPRUEBA de bocina: PA GPIO%d (activo alto)", pa);
    ESP_LOGW(TAG, "  Fase A: PA=1, tono 1 kHz (800 ms)");
    if (pa >= 0) {
        gpio_set_level(pa, 1);
        ESP_LOGI(TAG, "  GPIO%d leído = %d", pa, gpio_get_level(pa));
    }
    tono_simple(1000, 800, 0.7f);
    vTaskDelay(pdMS_TO_TICKS(300));

    if (pa >= 0) {
        ESP_LOGW(TAG, "  Fase B: PA=0, tono 1 kHz (800 ms)");
        gpio_set_level(pa, 0);
        tono_simple(1000, 800, 0.7f);
        vTaskDelay(pdMS_TO_TICKS(300));
        gpio_set_level(pa, 1);
    }

    ESP_LOGW(TAG, "  Fase C: PA=1, barrido 400/1000/2000 Hz");
    tono_simple(400, 400, 0.7f);
    vTaskDelay(pdMS_TO_TICKS(80));
    tono_simple(1000, 400, 0.7f);
    vTaskDelay(pdMS_TO_TICKS(80));
    tono_simple(2000, 400, 0.7f);

    esp_codec_dev_close(s_spk);
    xSemaphoreGive(s_lock);
    ESP_LOGW(TAG, "AUTOPRUEBA terminada. Si no se oyó nada, el problema es"
                  " físico: bocina desconectada, jack, o el amplificador.");
    return ESP_OK;
}

/* ------------------------------------------------------------------------- */
/* Reproducción de MP3 en streaming por HTTP                                 */
/* ------------------------------------------------------------------------- */

/* Escribe PCM al codec. Si el audio es mono de 16 bits lo duplica a estéreo
 * (el edge-tts entrega MP3 mono y la bocina de la placa es estéreo). */
static bool write_pcm(const uint8_t *data, size_t len, bool mono16, uint8_t **st_buf, size_t *st_cap)
{
    if (!mono16) {
        return esp_codec_dev_write(s_spk, (void *)data, (int)len) == ESP_CODEC_DEV_OK;
    }

    size_t need = len * 2;
    if (need > *st_cap) {
        uint8_t *nb = realloc(*st_buf, need);
        if (nb == NULL) {
            return false;
        }
        *st_buf = nb;
        *st_cap = need;
    }
    const int16_t *src = (const int16_t *)data;
    int16_t *dst = (int16_t *)(*st_buf);
    for (size_t i = 0; i < len / 2; i++) {
        dst[2 * i] = src[i];
        dst[2 * i + 1] = src[i];
    }
    return esp_codec_dev_write(s_spk, *st_buf, (int)need) == ESP_CODEC_DEV_OK;
}

esp_err_t gisee_audio_play_http_post(const char *url, const char *json_body)
{
    if (s_spk == NULL) {
        return ESP_ERR_INVALID_STATE;
    }

    xSemaphoreTake(s_lock, portMAX_DELAY);
    s_stop = false;
    s_playing = true;

    esp_err_t ret = ESP_OK;
    uint8_t *in_buf = malloc(READ_BUF_SIZE);
    uint8_t *out_buf = malloc(OUT_BUF_SIZE);
    size_t out_cap = OUT_BUF_SIZE;
    uint8_t *st_buf = NULL;
    size_t st_cap = 0;
    esp_audio_simple_dec_handle_t dec = NULL;
    esp_http_client_handle_t http = NULL;
    bool codec_open = false;
    bool mono16 = false;
    int eagain = 0;

    esp_audio_simple_dec_cfg_t dec_cfg = {
        .dec_type = ESP_AUDIO_SIMPLE_DEC_TYPE_MP3,
        .dec_cfg = NULL,
        .cfg_size = 0,
        .use_frame_dec = false,
    };
    esp_http_client_config_t http_cfg = {
        .url = url,
        .method = HTTP_METHOD_POST,
        .timeout_ms = 20000,
        .buffer_size = 2048,
    };

    if (in_buf == NULL || out_buf == NULL) {
        ESP_LOGE(TAG, "Sin memoria para los buffers");
        ret = ESP_ERR_NO_MEM;
        goto cleanup;
    }
    if (esp_audio_simple_dec_open(&dec_cfg, &dec) != ESP_AUDIO_ERR_OK) {
        ESP_LOGE(TAG, "No se pudo abrir el decodificador MP3");
        ret = ESP_FAIL;
        goto cleanup;
    }

    /* ---- Petición HTTP ---- */
    http = esp_http_client_init(&http_cfg);
    if (http == NULL) {
        ret = ESP_FAIL;
        goto cleanup;
    }
    esp_http_client_set_header(http, "Content-Type", "application/json");

    int body_len = (int)strlen(json_body);
    ret = esp_http_client_open(http, body_len);
    if (ret != ESP_OK) {
        ESP_LOGE(TAG, "No se pudo conectar a %s: %s", url, esp_err_to_name(ret));
        goto cleanup;
    }
    if (esp_http_client_write(http, json_body, body_len) != body_len) {
        ESP_LOGE(TAG, "Error enviando la petición");
        ret = ESP_FAIL;
        goto cleanup;
    }
    esp_http_client_fetch_headers(http);
    int status = esp_http_client_get_status_code(http);
    if (status != 200) {
        ESP_LOGE(TAG, "El servidor respondió HTTP %d", status);
        ret = ESP_FAIL;
        goto cleanup;
    }

    /* ---- Bucle: red -> decodificador -> codec ---- */
    while (!s_stop) {
        int n = esp_http_client_read(http, (char *)in_buf, READ_BUF_SIZE);
        if (n < 0) {
            if (n == -ESP_ERR_HTTP_EAGAIN && ++eagain < 3) {
                continue;
            }
            ESP_LOGE(TAG, "Error leyendo el audio (%d)", n);
            ret = ESP_FAIL;
            break;
        }
        if (n == 0) {
            break;                                   /* fin del stream */
        }
        eagain = 0;

        esp_audio_simple_dec_raw_t raw = {
            .buffer = in_buf,
            .len = (uint32_t)n,
            .eos = esp_http_client_is_complete_data_received(http),
        };

        while (raw.len > 0 && !s_stop) {
            esp_audio_simple_dec_out_t out = { .buffer = out_buf, .len = (uint32_t)out_cap };
            esp_audio_err_t err = esp_audio_simple_dec_process(dec, &raw, &out);

            if (err == ESP_AUDIO_ERR_BUFF_NOT_ENOUGH) {
                uint8_t *nb = realloc(out_buf, out.needed_size);
                if (nb == NULL) {
                    ret = ESP_ERR_NO_MEM;
                    break;
                }
                out_buf = nb;
                out_cap = out.needed_size;
                continue;
            }
            if (err != ESP_AUDIO_ERR_OK) {
                ESP_LOGW(TAG, "Error de decodificación (%d)", err);
                ret = ESP_FAIL;
                break;
            }

            if (out.decoded_size > 0) {
                if (!codec_open) {
                    esp_audio_simple_dec_info_t info = {0};
                    if (esp_audio_simple_dec_get_info(dec, &info) == ESP_AUDIO_ERR_OK) {
                        ESP_LOGI(TAG, "Formato: %lu Hz, %u bits, %u canal(es)",
                                 (unsigned long)info.sample_rate, info.bits_per_sample, info.channel);
                        mono16 = (info.channel == 1 && info.bits_per_sample == 16);

                        esp_codec_dev_sample_info_t fs = {
                            .sample_rate = info.sample_rate,
                            .bits_per_sample = info.bits_per_sample,
                            .channel = mono16 ? 2 : info.channel,
                        };
                        if (esp_codec_dev_open(s_spk, &fs) != ESP_CODEC_DEV_OK) {
                            ESP_LOGE(TAG, "No se pudo abrir el codec");
                            ret = ESP_FAIL;
                            break;
                        }
                        esp_codec_dev_set_out_vol(s_spk, CONFIG_GISEE_VOLUME);
                        pa_enable();
                        codec_open = true;
                    }
                }
                if (codec_open && !write_pcm(out.buffer, out.decoded_size, mono16, &st_buf, &st_cap)) {
                    ESP_LOGW(TAG, "Error escribiendo al codec");
                }
            }

            uint32_t consumed = raw.consumed;
            raw.len -= consumed;
            raw.buffer += consumed;
            if (consumed == 0 && out.decoded_size == 0) {
                break;                               /* evita bucle infinito */
            }
        }

        if (ret != ESP_OK || esp_http_client_is_complete_data_received(http)) {
            break;
        }
    }

cleanup:
    if (codec_open) {
        esp_codec_dev_close(s_spk);
    }
    if (dec != NULL) {
        esp_audio_simple_dec_close(dec);
    }
    if (http != NULL) {
        esp_http_client_close(http);
        esp_http_client_cleanup(http);
    }
    free(in_buf);
    free(out_buf);
    free(st_buf);

    s_playing = false;
    xSemaphoreGive(s_lock);
    return ret;
}

/* ------------------------------------------------------------------------- */
/* Atajos hacia el servidor                                                  */
/* ------------------------------------------------------------------------- */

static esp_err_t post_json(const char *path, const char *field, const char *value)
{
    char url[192];
    snprintf(url, sizeof(url), "%s%s", CONFIG_GISEE_SERVER_URL, path);

    cJSON *o = cJSON_CreateObject();
    if (o == NULL) {
        return ESP_ERR_NO_MEM;
    }
    cJSON_AddStringToObject(o, field, value);
    char *body = cJSON_PrintUnformatted(o);
    cJSON_Delete(o);
    if (body == NULL) {
        return ESP_ERR_NO_MEM;
    }

    ESP_LOGI(TAG, "POST %s", url);
    esp_err_t r = gisee_audio_play_http_post(url, body);
    cJSON_free(body);
    return r;
}

esp_err_t gisee_say(const char *texto)
{
    return post_json("/tts", "texto", texto);
}

esp_err_t gisee_chat(const char *mensaje)
{
    return post_json("/chat/audio", CONFIG_GISEE_CHAT_FIELD, mensaje);
}
