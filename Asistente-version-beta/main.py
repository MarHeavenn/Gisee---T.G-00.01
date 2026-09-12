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

from evaluator import evaluar_respuesta

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
    ("system", """Eres Gisee, se pronuncia yisi, eres un asistente emocional 
    divertido, chistoso, entusiasta, amable, amistoso, respetuoso y empático, 
    responde siempre en español diciendo algo para hacer sentir mucho mejor al usuario.
    
    Usa esta información de contexto para responder mejor:
    
    Información de documentos especializados:
    {contexto_pdfs}
    
    Conversaciones anteriores relevantes con este usuario:
    {memoria_anterior}
    """),
    # aquí se inyecta el historial de mensajes de esta sesión
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

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8000)