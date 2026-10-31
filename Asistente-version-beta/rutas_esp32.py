import os
import io
import tempfile
from fastapi import APIRouter, UploadFile, File, Form, Response
from pydub import AudioSegment
from groq import Groq
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

def inicializar_router(chain, historiales):
    """Guarda las referencias de main.py para poder usarlas aquí"""
    app_context['chain'] = chain
    app_context['historiales'] = historiales

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
                model="whisper-large-v3",
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
            voice="es-US-AlonsoNeural", 
            rate="+5%", 
            pitch="+10Hz"
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