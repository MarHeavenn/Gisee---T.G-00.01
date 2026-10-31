# FastAPI=framework web -> crear la API REST - genera el Swagger automáticamente
from fastapi import FastAPI, HTTPException

# BaseModel es de Pydantic — definir la "forma" de los datos que entran y salen del endpoint (como un contrato de datos)
from pydantic import BaseModel

# ChatGroq es el wrapper de LangChain compatible con la API de Groq
from langchain_groq import ChatGroq

# ChatPromptTemplate permite construir el prompt con roles:
# "system" (personalidad del bot) y "human" (mensaje del usuario)
from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder

# StrOutputParser convierte la respuesta de Grok (un objeto complejo) => string plano y legible
from langchain_core.output_parsers import StrOutputParser

from langchain_core.messages import HumanMessage, AIMessage

# load_dotenv lee el archivo .env y carga las variables de entorno
# para que os.getenv() las pueda encontrar en el código
from dotenv import load_dotenv

# os nos da acceso a las variables del sistema operativo
# lo usamos para leer XAI_API_KEY desde el .env
import os

###################################

from evaluator import evaluar_respuesta
from fastapi.responses import HTMLResponse

from fastapi.responses import StreamingResponse
from gtts import gTTS
import io

from fastapi import UploadFile, File
import tempfile
from groq import Groq
from tts_utils import limpiar_texto_para_tts
from fastapi import Form, Response
from pydub import AudioSegment

from rutas_esp32 import router as esp32_router, inicializar_router
    
# Ejecuta load_dotenv() para que las variables del .env
# queden disponibles en este proceso
load_dotenv()

# importamos las funciones de memoria y RAG
from memory import guardar_en_memoria, buscar_en_memoria
from rag import buscar_contexto

# ------- LLM -------

# Le decimos a LangChain qué modelo usar y dónde encontrarlo
# model: nombre exacto del modelo de xAI
# api_key: clave del .env
# base_url: apunta a los servidores de xAI en lugar de OpenAI
llm = ChatGroq(
    model="qwen/qwen3.8-27b",  # modelo de xAI
    api_key=os.getenv("GROQ_API_KEY"),
)

# ------- Prompt -------
# Definimos la "personalidad" y estructura del chat
# from_messages = recibe una lista de tuplas (rol, contenido)
# "system" -> instrucciones para el bot (personalidad de Gisee)
# "human" -> mensaje del usuario
# {MensajeUsuario} -> placeholder que se rellena en tiempo de ejecución

# el prompt ahora tiene tres fuentes de información:
# 1. la personalidad de Gisee
# 2. contexto de PDFs y memoria anterior (se inyecta en system)
# 3. el historial de mensajes de esta sesión
# 4. el mensaje actual del usuario
prompt = ChatPromptTemplate.from_messages([
    ("system", """Eres Gisee, un asistente emocional masculino. 
    Eres divertido, chistoso, entusiasta, amable, amistoso, respetuoso y empático.
    Responde siempre en español diciendo algo para hacer sentir mucho mejor al usuario.
    
    REGLAS ESTRICTAS: 
    1. No uses ningún emoji, emoticono o símbolo especial. 
    2. Usa signos de exclamación (¡!) y puntos suspensivos (...) para darle emoción y pausas naturales a tus frases, ya que serás leído en voz alta.

    REGLA DE ORATORIA: Escribe en oraciones cortas. Usa abundantes comas, puntos y signos de exclamación (¡!) para marcar pausas y dar énfasis emocional. Nunca escribas párrafos largos sin pausas.
    
    Información de documentos especializados:
    {contexto_pdfs}
    
    Conversaciones anteriores relevantes con este usuario:
    {memoria_anterior}
    """),
    MessagesPlaceholder(variable_name="historial"),
    ("human", "{MensajeUsuario}"),
])

# ------- Cadena LangChain -------
# El operador | encadena pasos en orden como una tubería (pipe)
# 1. prompt -> construye el mensaje con el placeholder relleno
# 2. llm -> envía el mensaje a Grok y obtiene la respuesta
# 3. StrOutputParser -> convierte esa respuesta a string simple
chain = prompt | llm | StrOutputParser()

# ------- FastAPI -------
# Crea la app FastAPI
# title, description y version aparecen en la página de Swagger (/docs)
app = FastAPI(
    title="Asistente Emocional Gisee",
    description="Un asistente emocional divertido y empático con FastAPI + LangChain + Groq.",
    version="1.0.0",
)

# historial en memoria RAM para la sesión activa
# guarda los últimos mensajes como objetos HumanMessage y AIMessage
historiales = {}

# ------- Schemas -------
# ChatRequest -> datos a recibir en el Endpoint
# Pydantic valida automáticamente que el JSON tenga este campo
# Si el cliente manda algo incorrecto, FastAPI responde 422 automáticamente
class ChatRequest(BaseModel):
    session_id: str  # para identificar la sesión y su historial
    MensajeUsuario: str  # manda un campo de tipo string

# ChatResponse -> datos a enviar desde el Endpoint
# Swagger lo usa para mostrar el ejemplo de respuesta en /docs
class ChatResponse(BaseModel):
    respuesta_bot: str  # bot responde en el campo "respuesta_bot"

# ------- Endpoint -------
# @app.post("/chat") registra esta función como endpoint HTTP POST en la ruta /chat
# response_model=ChatResponse le dice a FastAPI cómo serializar y documentar la salida
@app.post("/chat", response_model=ChatResponse)
def chat(request: ChatRequest):
    try:
        # 1. trae o crea el historial RAM de esta sesión
        if request.session_id not in historiales:
            historiales[request.session_id] = []
        historial = historiales[request.session_id]

        # 2. busca fragmentos relevantes en los PDFs
        contexto_pdfs = buscar_contexto(request.MensajeUsuario)

        # 3. busca mensajes anteriores relevantes en ChromaDB
        memoria_anterior = buscar_en_memoria(
            request.session_id, #Dato 1
            request.MensajeUsuario #Dato 2
        )

        # 4. invoca la cadena con todo el contexto junto
        respuesta_bot = chain.invoke({
            "historial": historial,
            "MensajeUsuario": request.MensajeUsuario,
            "contexto_pdfs": contexto_pdfs,
            "memoria_anterior": memoria_anterior,
        })

        # 5. guarda el mensaje y la respuesta en el historial RAM
        historial.append(HumanMessage(content=request.MensajeUsuario))
        historial.append(AIMessage(content=respuesta_bot))

        # 6. guarda el mensaje del usuario en ChromaDB para memoria futura
        guardar_en_memoria(request.session_id, request.MensajeUsuario)
        # guarda también la respuesta para que Gisee recuerde lo que dijo
        guardar_en_memoria(request.session_id, f"Gisee respondió: {respuesta_bot}")
        # 7. evalua al respuesta antes de enviarla 
        respuesta_evaluada = evaluar_respuesta(request.MensajeUsuario, respuesta_bot)
        return ChatResponse(respuesta_bot=respuesta_evaluada)   

    except Exception as e:
        # Si algo falla (cuota, red, etc.), responde con un mensaje amable
        # en lugar de explotar con un error 500
        raise HTTPException(
            status_code=503,
            detail=f"Gisee no está disponible: {str(e)}"
        )
    
# endpoint que recibe audio y lo transcribe con Whisper de Groq
@app.post("/transcribir")
async def transcribir(audio: UploadFile = File(...)):
    cliente_groq = Groq(api_key=os.getenv("GROQ_API_KEY"))
    
    # guarda el audio temporalmente en disco
    with tempfile.NamedTemporaryFile(delete=False, suffix=".webm") as tmp:
        contenido = await audio.read()
        tmp.write(contenido)
        tmp_path = tmp.name
    
    try:
        # envía el audio a Whisper de Groq para transcribirlo
        with open(tmp_path, "rb") as f:
            transcripcion = cliente_groq.audio.transcriptions.create(
                model="whisper-large-v3",
                file=f,
                language="es",
            )
        return {"texto": transcripcion.text}
    finally:
        # borra el archivo temporal
        os.unlink(tmp_path)

# endpoint que convierte texto a audio con gTTS y lo devuelve al navegador
# endpoint que convierte texto a audio con Edge TTS (voz de hombre colombiano)
@app.post("/hablar")
async def hablar(request: dict):
    import edge_tts

    texto = request.get("texto", "")
    # print temporal para ver qué texto llega al TTS
    print(f"[TTS] Texto: '{texto}'")
     # limpia el texto antes de enviarlo al TTS
    texto = limpiar_texto_para_tts(texto)
    # es-CO-GonzaloNeural = voz de hombre colombiano de Microsoft
    # puedes cambiarla por es-ES-AlvaroNeural (España) o es-MX-JorgeNeural (México)
    comunicar = edge_tts.Communicate(
        text=texto, 
        voice="es-CO-GonzaloNeural", 
        rate="+5%",   # Habla un pelín más rápido para darle dinamismo
        pitch="+30Hz" # Sube el tono para que suene más entusiasta y menos plano
    )

    # guarda el audio en memoria RAM
    audio_buffer = io.BytesIO()
    
    # genera el audio fragmento por fragmento y lo va guardando
    async for chunk in comunicar.stream():
        if chunk["type"] == "audio":
            audio_buffer.write(chunk["data"])
    
    audio_buffer.seek(0)
    
    # devuelve el audio como stream al navegador
    return StreamingResponse(
        audio_buffer,
        media_type="audio/mpeg"
    )

@app.get("/", response_class=HTMLResponse)
async def get_chat_page():
    with open("chat.html", "r", encoding="utf-8") as f:
        return f.read()
    
# Importar el router del nuevo archivo
from rutas_esp32 import router as router_esp32, inicializar_router

# Pasarle la cadena y el historial al router para que los pueda usar
inicializar_router(chain, historiales)

# Conectar el router a la app principal de FastAPI
app.include_router(router_esp32)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)