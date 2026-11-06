# Gisee — la voz de la IA sale por la bocina del ESP32-P4

Proyecto ESP-IDF para la placa **JC-ESP32P4-M3-DEV**. El ESP32-P4 le envía el
mensaje al servidor FastAPI (**Groq** como LLM + **RAG** de psicología, repo del
backend: https://github.com/MarHeavenn/Gisee---T.G-00.01), el servidor responde
con **MP3 en streaming** y la placa lo decodifica y reproduce en tiempo real,
sin guardarlo en SD.

```
ESP32-P4  --(WiFi por el C6, ESP-Hosted)-->  FastAPI (Groq + RAG + edge-tts)
    ^                                                |
    +------------ decodifica MP3 en streaming -------+
```

## Estructura

| Archivo              | Qué hace                                                              |
|----------------------|-----------------------------------------------------------------------|
| `main/main.c`        | NVS, arranque de audio/red y tarea principal + botón BOOT (GPIO35)     |
| `main/gisee_audio.c` | Codec ES8311, PA NS4150 (PA_CTRL = GPIO11), I2S con pinout real (DIN = GPIO48) y decodificador MP3 simple en streaming |
| `main/gisee_net.c`   | WiFi en modo estación con reconexión automática                       |
| `main/Kconfig.projbuild` | Configuración (menuconfig → "Gisee")                              |
| `server/tts_audio.py`| Router FastAPI que convierte texto en MP3 (edge-tts) para copiar a tu backend |

## Configuración

```sh
idf.py menuconfig        # dentro de "Gisee (voz de la IA)":
```
- **SSID y contraseña** del WiFi
- **URL base** del servidor FastAPI (ej. `http://192.168.1.50:8000`)
- **GPIO del PA**: `11` (PA_CTRL del NS4150 en la JC-ESP32P4-M3-DEV).
  La BSP `esp32_p4_function_ev_board` usa el pinout de otra placa (PA = GPIO53,
  DSIN = GPIO11), así que `gisee_audio.c` inicializa el I2S con el pinout real
  del esquemático de GUITION: MCLK 13, SCLK 12, LRCK 10, DOUT 9 y DIN 48.
- **volumen** (0-100)
- **Campo JSON** que espera `/chat/audio` (`MensajeUsuario` por defecto)

## Parte del servidor (FastAPI)

1. `pip install edge-tts`
2. Copia `server/tts_audio.py` al proyecto del backend y regístralo en `main.py`:
   ```python
   from tts_audio import router as tts_router
   app.include_router(tts_router)          # añade POST /tts
   ```
3. Para `/chat/audio`, añade el endpoint indicado al final de `server/tts_audio.py`,
   reutilizando el mismo modelo de petición que ya usa tu `/chat` y envolviendo la
   respuesta con `mp3_stream(...)`.

## Compilar y flashear

```sh
idf.py set-target esp32p4
idf.py build flash monitor
```

Al arrancar suena un pitido (comprueba la bocina sin red). Si el SSID/contraseña
son correctos dirá "Hola, soy Gisee. Ya estoy listo para acompañarte. Cuéntame,
¿cómo te sientes hoy?" vía `/tts`. El botón **BOOT (GPIO35)**:

- **mantener pulsado** → graba tu voz (hasta 10 s) por el micrófono
- **al soltar** → manda el WAV a `/stt`, el texto a `/chat/audio` y reproduce la respuesta
- **toque corto mientras habla** → corta la reproducción

Los gestos están en orden: si no suena nada, primero prueba el pitido sin red.

## Si no se oye nada (diagnóstico)

Al arrancar el firmware da un **pitido corto** (sin red) y luego saluda por
`/tts`. Si hace falta diagnosticar la bocina, en `main/main.c` cambia el pitido
por `gisee_audio_selftest()`: toca tres fases (PA=1, PA=0 y barrido) e imprime
los registros del ES8311 (mute/volumen del DAC):

```
AUTOPRUEBA de bocina: PA GPIO11 (activo alto)
  Fase A: PA=1, tono 1 kHz (800 ms)
  Fase B: PA=0, tono 1 kHz (800 ms)
  Fase C: PA=1, barrido 400/1000/2000 Hz
ES8311 DAC control [0x31] = 0x00   -> DAC sin mute
ES8311 DAC volumen [0x32] = 0xC6   -> volumen alto
ES8311 system [0x0D] = 0x01        -> analógico encendido
```

Si con PA=1 no se oye nada pero con PA=0 sí, el enable está invertido. Si no se
oye ninguna fase, el problema es físico: bocina desconectada, conector
equivocado, jack de audífonos o el amplificador NS4150 sin alimentación.

## Supervisión del backend

El servidor FastAPI publica un panel en `http://127.0.0.1:8000/panel` con
peticiones, errores, avisos, registro en vivo y el estado de la placa. Desde la
raíz del backend:

```powershell
.\servidor.ps1 iniciar     # arranca en 0.0.0.0:8000
.\servidor.ps1 estado      # resumen por consola
.\servidor.ps1 logs        # registro en vivo
.\servidor.ps1 panel       # abre el panel en el navegador
.\servidor.ps1 detener     # lo apaga
```

