"""
Audio de Gisee: texto -> MP3 en streaming, para que lo reproduzca el ESP32-P4.

Instalar:   pip install edge-tts
Registrar en main.py:

    from tts_audio import router as tts_router
    app.include_router(tts_router)            # añade POST /tts
"""
import re

import edge_tts
from fastapi import APIRouter
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

router = APIRouter()

VOZ = "es-CO-SalomeNeural"      # voz colombiana; alternativa: es-CO-GonzaloNeural
VELOCIDAD = "-5%"               # un poco más pausada, más cálida

_EMOJIS = re.compile("[\U00010000-\U0010ffff\u2600-\u27bf\ufe0f\u200d]")


def limpiar_para_voz(texto: str) -> str:
    """Quita markdown y emojis para que la voz no los lea."""
    texto = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", texto)   # [texto](url) -> texto
    texto = re.sub(r"[*_`#>~]+", "", texto)
    texto = _EMOJIS.sub("", texto)
    return re.sub(r"\s+", " ", texto).strip()


async def mp3_stream(texto: str):
    """Generador asíncrono de trozos MP3 (24 kHz, mono)."""
    comunicar = edge_tts.Communicate(limpiar_para_voz(texto), VOZ, rate=VELOCIDAD)
    async for trozo in comunicar.stream():
        if trozo["type"] == "audio":
            yield trozo["data"]


class PeticionTTS(BaseModel):
    texto: str


@router.post("/tts")
async def tts(p: PeticionTTS):
    return StreamingResponse(mp3_stream(p.texto), media_type="audio/mpeg")


# ---------------------------------------------------------------------------
# Para /chat/audio añade esto en main.py, junto a tu endpoint /chat actual.
# Usa el MISMO modelo de petición y la MISMA lógica (Groq + RAG) que /chat,
# y en vez de devolver el texto lo convierte en voz:
#
# @app.post("/chat/audio")
# async def chat_audio(peticion: PeticionChat):        # <- el modelo que ya usa /chat
#     respuesta = <texto que hoy devuelve /chat>        # <- tu función actual
#     return StreamingResponse(mp3_stream(respuesta), media_type="audio/mpeg")
# ---------------------------------------------------------------------------
