"""
Servidor de PRUEBA para validar la placa JC-ESP32P4-M3-DEV con Gisee.

Igual contrato que el backend real (repo Gisee---T.G-00.01):
  POST /stt         (WAV en el cuerpo)      -> {"texto": "..."}
  POST /tts         {"texto": "..."}        -> MP3 en streaming (edge-tts)
  POST /chat/audio  {"MensajeUsuario": "..."} -> MP3 en streaming

Configurar GROQ_API_KEY (env. var) para usar Whisper real; sin ella, /stt
devuelve una frase fija para probar el circuito hardware.
"""
import os

import fastapi
import edge_tts
from fastapi.responses import StreamingResponse
from fastapi import Request
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel

from tts_audio import router, mp3_stream

app = fastapi.FastAPI()
app.include_router(router)

GROQ_KEY = os.environ.get("GROQ_API_KEY", "")


@app.post("/stt")
async def stt(request: Request):
    """Acepta el WAV crudo del ESP32 (también multipart, para probar con curl)."""
    content_type = request.headers.get("content-type", "")
    if content_type.startswith("multipart/form-data"):
        formulario = await request.form()
        archivo = formulario.get("audio") or formulario.get("file")
        data = await archivo.read() if archivo is not None else b""
    else:
        data = await request.body()
    print(f"[stt] recibidos {len(data)} bytes ({content_type or 'sin content-type'})")

    if not GROQ_KEY:
        # Sin API key: frase fija para validar el circuito completo
        return {"texto": "estoy escuchando bien tu voz, ya te puedo entender"}

    import requests

    def _whisper():
        r = requests.post(
            "https://api.groq.com/openai/v1/audio/transcriptions",
            headers={"Authorization": f"Bearer {GROQ_KEY}"},
            files={"file": ("grabacion.wav", data, "audio/wav")},
            data={"model": "whisper-large-v3-turbo"},
            timeout=60,
        )
        return r.json().get("text", "")

    texto = await run_in_threadpool(_whisper)
    print(f"[stt] Whisper: {texto!r}")
    return {"texto": texto}


class PeticionChat(BaseModel):
    MensajeUsuario: str


@app.post("/chat/audio")
async def chat_audio(p: PeticionChat):
    # AQUÍ iría tu lógica Groq + RAG. Por ahora devuelve una respuesta fija
    # para verificar que la placa reproduce el audio de punta a punta.
    respuesta = ("Entiendo que hoy te sientes triste. No estás solo: cuéntame "
                 "un poco más de lo que pasó, y vamos a verlo juntos paso a paso.")
    print(f"[chat/audio] recibido: {p.MensajeUsuario!r} -> respuesta fija")
    return StreamingResponse(mp3_stream(respuesta), media_type="audio/mpeg")

