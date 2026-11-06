/*
 * Audio de Gisee: reproduce por la bocina el MP3 que entrega el servidor
 * (FastAPI + Groq + RAG + TTS) en streaming, sin guardarlo en la SD.
 *
 * Hardware (JC-ESP32P4-M3-DEV): ES8311 por I2S -> NS4150 (PA_CTRL = GPIO11).
 */
#pragma once

#include <stdbool.h>
#include "esp_err.h"

/* Inicializa el codec ES8311, el amplificador y los decodificadores. */
esp_err_t gisee_audio_init(void);

/* Inicializa el micrófono (entrada analógica del ES8311). Llamar tras
 * gisee_audio_init(). */
esp_err_t gisee_audio_mic_init(void);

/* Lee audio mono de 16 bits @16 kHz del micrófono. Abre el codec la primera
 * vez; gisee_audio_mic_close() lo cierra al terminar la grabación. Devuelve
 * bytes leídos (múltiplos de 2) o -1 en error. */
int gisee_audio_mic_read_mono16(uint8_t *dst, int bytes);

/* Cierra el micrófono (libera el I2S RX). Llamar al terminar de grabar. */
void gisee_audio_mic_close(void);

/* POST /stt con un WAV; la respuesta es JSON {"texto": "..."}. El texto
 * transcrito se copia en `texto` (capacidad `texto_cap`). */
esp_err_t gisee_stt(const uint8_t *wav, size_t wav_len, char *texto, size_t texto_cap);

/* Volumen 0..100. */
void gisee_audio_set_volume(int volume);

/* Pitido de prueba (no necesita red). */
esp_err_t gisee_audio_beep(int freq_hz, int ms);

/* Autoprueba de la salida: imprime los registros del ES8311 y toca tonos con
 * el amplificador activado y desactivado (para descartar polaridad del PA).
 * No necesita red. */
esp_err_t gisee_audio_selftest(void);

/* POST con cuerpo JSON a `url`; la respuesta debe ser audio MP3. Se decodifica
 * y se reproduce mientras llega. Bloqueante hasta que termina o se llama a
 * gisee_audio_stop(). */
esp_err_t gisee_audio_play_http_post(const char *url, const char *json_body);

/* Corta la reproducción en curso. */
void gisee_audio_stop(void);
bool gisee_audio_is_playing(void);

/* Atajos contra el servidor configurado en CONFIG_GISEE_SERVER_URL: */
esp_err_t gisee_say(const char *texto);      /* POST /tts        {"texto": ...}   */
esp_err_t gisee_chat(const char *mensaje);   /* POST /chat/audio {"MensajeUsuario": ...} */
