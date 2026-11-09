import os
import io
import re
import difflib
import tempfile
import unicodedata
from fastapi import APIRouter, UploadFile, File, Form, Response, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel
from pydub import AudioSegment
from groq import Groq, RateLimitError
from langchain_core.messages import HumanMessage, AIMessage

# Importamos las herramientas de tu proyecto
from memory import guardar_en_memoria, buscar_en_memoria
from rag import buscar_contexto
from evaluator import evaluar_respuesta
from tts_utils import limpiar_texto_para_tts

# Creamos un "mini-servidor" de rutas
router = APIRouter()

# Variable global para inyectar la chain y el historial desde main.py
app_context = {}

# Voz única para el navegador y para el ESP32 (hombre colombiano)
VOZ_TTS = "es-CO-GonzaloNeural"
TTS_RATE = "+5%"
TTS_PITCH = "+30Hz"
MAX_HISTORIAL = 20

def inicializar_router(chain, historiales):
    """Guarda las referencias de main.py para poder usarlas aquí"""
    app_context['chain'] = chain
    app_context['historiales'] = historiales


def _responder_gisee(session_id: str, texto_usuario: str) -> str:
    """RAG + memoria + LLM + juez. Devuelve el texto que debe decir Gisee."""
    chain = app_context['chain']
    historiales = app_context['historiales']

    if session_id not in historiales:
        historiales[session_id] = []
    historial = historiales[session_id]

    # mismo límite que /chat para no crecer sin control
    if len(historial) > MAX_HISTORIAL:
        del historial[:-MAX_HISTORIAL]

    contexto_pdfs = buscar_contexto(texto_usuario)
    memoria_anterior = buscar_en_memoria(session_id, texto_usuario)

    respuesta_bot = chain.invoke({
        "historial": historial,
        "MensajeUsuario": texto_usuario,
        "contexto_pdfs": contexto_pdfs,
        "memoria_anterior": memoria_anterior,
    })

    historial.append(HumanMessage(content=texto_usuario))
    historial.append(AIMessage(content=respuesta_bot))
    guardar_en_memoria(session_id, texto_usuario)
    guardar_en_memoria(session_id, f"Gisee respondió: {respuesta_bot}")

    return evaluar_respuesta(texto_usuario, respuesta_bot)


async def _mp3_stream(texto: str):
    """Genera el MP3 con edge-tts y lo entrega según llega.

    Antes se acumulaba todo el MP3 en memoria antes de responder: la placa se
    quedaba en silencio todo el tiempo de síntesis (varios segundos con
    respuestas largas). En streaming el primer trozo sale de inmediato.
    """
    import edge_tts

    limpio = limpiar_texto_para_tts(texto)
    if not limpio.strip():
        return

    comunicar = edge_tts.Communicate(
        text=limpio,
        voice=VOZ_TTS,
        rate=TTS_RATE,
        pitch=TTS_PITCH,
    )
    async for chunk in comunicar.stream():
        if chunk["type"] == "audio":
            yield chunk["data"]


async def _texto_a_mp3(texto: str) -> bytes:
    """Versión buffered (se mantiene para usos que necesiten el MP3 completo)."""
    trozos = []
    async for trozo in _mp3_stream(texto):
        trozos.append(trozo)
    return b"".join(trozos)


def _transcribir_bytes(audio: bytes, suffix: str = ".wav") -> str:
    """Manda los bytes de audio a Whisper (Groq) y devuelve el texto."""
    api_key = os.getenv("GROQ_API_KEY")
    if not api_key:
        raise HTTPException(status_code=503, detail="Falta GROQ_API_KEY en el .env")

    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
        tmp.write(audio)
        ruta = tmp.name

    try:
        cliente = Groq(api_key=api_key)
        with open(ruta, "rb") as f:
            transcripcion = cliente.audio.transcriptions.create(
                model="whisper-large-v3-turbo",
                file=f,
                language="es",
            )
        return transcripcion.text
    finally:
        if os.path.exists(ruta):
            os.unlink(ruta)

@router.post("/api/esp32/interact")
async def esp32_interact(
    client_id: str = Form(...),
    audio_file: UploadFile = File(...)
):
    chain = app_context['chain']
    historiales = app_context['historiales']

    # 1. Guardar temporalmente el WAV enviado por el ESP32
    with tempfile.NamedTemporaryFile(delete=False, suffix=".wav") as tmp_in:
        contenido = await audio_file.read()
        tmp_in.write(contenido)
        tmp_in_path = tmp_in.name

    try:
        # 2. Transcribir con Whisper de Groq
        cliente_groq = Groq(api_key=os.getenv("GROQ_API_KEY"))
        with open(tmp_in_path, "rb") as f:
            transcripcion = cliente_groq.audio.transcriptions.create(
                model="whisper-large-v3-turbo",
                file=f,
                language="es",
            )
        texto_usuario = transcripcion.text
        print(f"[ESP32] Usuario ({client_id}): {texto_usuario}")

        # 3. Lógica de LangChain (RAG y Memoria)
        if client_id not in historiales:
            historiales[client_id] = []
        historial = historiales[client_id]

        contexto_pdfs = buscar_contexto(texto_usuario)
        memoria_anterior = buscar_en_memoria(client_id, texto_usuario)

        respuesta_bot = chain.invoke({
            "historial": historial,
            "MensajeUsuario": texto_usuario,
            "contexto_pdfs": contexto_pdfs,
            "memoria_anterior": memoria_anterior,
        })

        historial.append(HumanMessage(content=texto_usuario))
        historial.append(AIMessage(content=respuesta_bot))
        guardar_en_memoria(client_id, texto_usuario)
        guardar_en_memoria(client_id, f"Gisee respondió: {respuesta_bot}")

        # 4. Evaluación del Juez
        respuesta_evaluada = evaluar_respuesta(texto_usuario, respuesta_bot)
        print(f"[ESP32] Gisee: {respuesta_evaluada}")

        # 5. Generar Audio (Edge TTS)
        import edge_tts
        texto_limpio = limpiar_texto_para_tts(respuesta_evaluada)
        
        # Voz masculina con ajustes de tono
        comunicar = edge_tts.Communicate(
            text=texto_limpio,
            voice=VOZ_TTS,
            rate=TTS_RATE,
            pitch=TTS_PITCH,
        )

        audio_mp3_buffer = io.BytesIO()
        async for chunk in comunicar.stream():
            if chunk["type"] == "audio":
                audio_mp3_buffer.write(chunk["data"])
        audio_mp3_buffer.seek(0)

        # 6. Convertir MP3 de EdgeTTS a WAV limpio de 16 bits para el ESP32
        audio_segment = AudioSegment.from_file(audio_mp3_buffer, format="mp3")
        
        # forzar 16000 Hz mono 16 bits — exactamente lo que espera el ESP32
        audio_segment = audio_segment.set_frame_rate(16000)
        audio_segment = audio_segment.set_channels(1)
        audio_segment = audio_segment.set_sample_width(2)  # 2 bytes = 16 bits
        
        # exportar a WAV PCM estándar
        wav_buffer = io.BytesIO()
        audio_segment.export(wav_buffer, format="wav", codec="pcm_s16le")
        wav_buffer.seek(0)

        return Response(content=wav_buffer.getvalue(), media_type="audio/wav")

    finally:
        if os.path.exists(tmp_in_path):
            os.unlink(tmp_in_path)


# ---------------------------------------------------------------------------
# Protocolo del firmware ESP32-P4 (Gisee_Hardware)
#   POST /stt          cuerpo = WAV crudo            -> {"texto": "..."}
#   POST /tts          {"texto": "..."}              -> MP3
#   POST /chat/audio   {"MensajeUsuario": "..."}     -> MP3
# ---------------------------------------------------------------------------

class PeticionTTS(BaseModel):
    texto: str


class PeticionChatAudio(BaseModel):
    MensajeUsuario: str
    session_id: str = "esp32"


@router.post("/stt")
async def esp32_stt(request: Request):
    """Transcribe el WAV que manda la placa.

    El firmware envía el WAV como cuerpo crudo (Content-Type: audio/wav).
    También aceptamos multipart/form-data por comodidad al probar con curl.
    """
    content_type = request.headers.get("content-type", "")
    if content_type.startswith("multipart/form-data"):
        formulario = await request.form()
        archivo = formulario.get("audio") or formulario.get("file")
        if archivo is None:
            raise HTTPException(status_code=400, detail="Falta el archivo 'audio'")
        audio = await archivo.read()
        suffix = ".wav"
    else:
        audio = await request.body()
        suffix = ".webm" if "webm" in content_type else ".wav"

    if not audio:
        raise HTTPException(status_code=400, detail="El audio llegó vacío")

    print(f"[ESP32] /stt: {len(audio)} bytes ({content_type or 'sin content-type'})")
    texto = await run_in_threadpool(_transcribir_bytes, audio, suffix)
    print(f"[ESP32] Transcripción: {texto!r}")
    return {"texto": texto}


# ---------------------------------------------------------------------------
# Activación por voz: "hola Gisee"
# ---------------------------------------------------------------------------
# La placa no tiene un modelo WakeNet en español, así que la detección se hace
# aquí: el firmware manda un fragmento de voz cuando detecta que alguien habló
# y el servidor comprueba si dijo el nombre. Whisper transcribe "Gisee" de
# formas muy distintas ("Gisí", "Yisee", "Giselle"...), por eso comparamos por
# similitud en vez de exigir la palabra exacta.

_SALUDOS_WAKE = {"hola", "oye", "hey", "ey", "buenas", "buenos", "dias", "tardes", "noches"}

# Whisper transcribe el nombre de muchas formas. Estas son las más comunes en
# español; el resto se resuelve por similitud.
_VARIANTES_WAKE = {
    "gisee", "gise", "gisi", "gisy", "gisse", "giseel", "gizee", "gize",
    "yisee", "yisi", "jisee", "jisi", "gicee", "giselle", "gisela",
}


def _normalizar(texto: str) -> str:
    """Minúsculas, sin tildes y sin signos, para comparar la frase de activación."""
    t = unicodedata.normalize("NFKD", (texto or "").lower())
    t = "".join(c for c in t if not unicodedata.combining(c))
    t = re.sub(r"[^a-z0-9ñ ]+", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def _parece_gisee(palabra: str) -> bool:
    if palabra in _VARIANTES_WAKE:
        return True
    if 5 <= len(palabra) <= 8:
        return difflib.SequenceMatcher(None, palabra, "gisee").ratio() >= 0.8
    return False


def _detectar_activacion(texto: str):
    """Devuelve (activado, resto): qué dijo la persona después del nombre."""
    palabras = _normalizar(texto).split()
    for i, palabra in enumerate(palabras):
        if _parece_gisee(palabra):
            resto = [w for w in palabras[i + 1:] if w not in _SALUDOS_WAKE]
            return True, " ".join(resto)
    return False, ""


@router.post("/wake")
async def esp32_wake(request: Request):
    """Detecta "hola Gisee" en el fragmento de voz que manda la placa.

    Devuelve {"wake": bool, "texto": "...", "resto": "..."}, donde `resto` es
    lo dicho después del nombre: si la persona dijo "hola Gisee, me siento
    triste" se responde directo, sin volver a preguntarle qué quiere.
    """
    content_type = request.headers.get("content-type", "")
    if content_type.startswith("multipart/form-data"):
        formulario = await request.form()
        archivo = formulario.get("audio") or formulario.get("file")
        if archivo is None:
            raise HTTPException(status_code=400, detail="Falta el archivo 'audio'")
        audio = await archivo.read()
    else:
        audio = await request.body()

    if not audio:
        raise HTTPException(status_code=400, detail="El audio llegó vacío")

    texto = await run_in_threadpool(_transcribir_bytes, audio, ".wav")
    activado, resto = _detectar_activacion(texto)
    print(f"[ESP32] /wake: {texto!r} -> {'ACTIVADO' if activado else 'sin activación'}")
    return {"wake": activado, "texto": texto, "resto": resto}


@router.post("/tts")
async def esp32_tts(peticion: PeticionTTS):
    """Texto -> MP3 para que la placa lo reproduzca al arrancar.

    Se genera el MP3 completo antes de responder. Edge TTS tarda mucho menos
    que el tiempo real, así que la espera extra es pequeña; a cambio la placa
    recibe todos los bytes seguidos y no se producen cortes por los huecos
    del WebSocket de síntesis.
    """
    if not peticion.texto.strip():
        raise HTTPException(status_code=400, detail="El texto está vacío")
    audio = await _texto_a_mp3(peticion.texto)
    if not audio:
        raise HTTPException(status_code=502, detail="El TTS no generó audio")
    return Response(content=audio, media_type="audio/mpeg")


@router.post("/chat/audio")
async def esp32_chat_audio(peticion: PeticionChatAudio):
    """Mensaje del usuario -> RAG + LLM + juez -> MP3 (en streaming)."""
    texto_usuario = peticion.MensajeUsuario.strip()
    if not texto_usuario:
        raise HTTPException(status_code=400, detail="El mensaje está vacío")

    session_id = peticion.session_id or "esp32"
    print(f"[ESP32] Usuario ({session_id}): {texto_usuario}")

    # chain.invoke + Chroma son bloqueantes: no detener el event loop.
    try:
        respuesta = await run_in_threadpool(_responder_gisee, session_id, texto_usuario)
    except RateLimitError as e:
        print(f"[ESP32] Límite de tokens de Groq alcanzado: {e}")
        raise HTTPException(
            status_code=429,
            detail="Límite de tokens de Groq alcanzado; intenta de nuevo en un minuto",
        )
    print(f"[ESP32] Gisee: {respuesta}")

    # Igual que en /tts: MP3 completo antes de responder. Con streaming, un
    # hueco de la síntesis se oía como un silencio de varios segundos a mitad
    # de la frase porque el I2S de la placa se queda sin datos.
    audio = await _texto_a_mp3(respuesta)
    if not audio:
        raise HTTPException(status_code=502, detail="El TTS no generó audio")
    return Response(content=audio, media_type="audio/mpeg")