# FastAPI=framework web -> crear la API REST - genera el Swagger automáticamente
from fastapi import FastAPI

# BaseModel es de Pydantic — definir la "forma" de los datos que entran y salen del endpoint (como un contrato de datos)
from httpcore import request
from pydantic import BaseModel

# El LLM de Gemini envuelto para que LangChain pueda usarlo
# Sin esto: LangChain no sabe cómo hablarle a Gemini
from langchain_google_genai import ChatGoogleGenerativeAI

# ChatPromptTemplate permite construir el prompt con roles:
# "system" (personalidad del bot) y "human" (mensaje del usuario)
from langchain_core.prompts import ChatPromptTemplate

# StrOutputParser convierte la respuesta de Gemini (un objeto complejo) =>string plano y legible
from langchain_core.output_parsers import StrOutputParser

# load_dotenv lee el archivo .env y carga las variables de entorno
# para que os.getenv() las pueda encontrar en el código
from dotenv import load_dotenv

# os nos da acceso a las variables del sistema operativo
# lo usamos para leer GEMINI_API_KEY desde el .env
import os

# Ejecuta load_dotenv() para que las variables del .env
# queden disponibles en este proceso
load_dotenv()

# ------- LLM -------

# LangChain se le dice modelo y credenciales
# model: nombre exacto
# google_api_key: clave del .env
llm = ChatGoogleGenerativeAI(
    model="gemini-2.0-flash",
    google_api_key=os.getenv("GEMINI_API_KEY"),
)

# ------- Prompt -------
# Definimos la "personalidad" y estructura del chat
# from_messages = recibe - lista de tublas (rol, contenido)
# "system" -> instrucciones para el bot (ej: "eres un asistente útil")
# "human" -> mensaje del usuario (ej: "{input}" es un placeholder que se reemplaza con el mensaje real)
# {mensaje} -> placeholder se rellena en tiempo de ejecución

prompt = ChatPromptTemplate.from_messages([
    ("system", "Eres Gisee, se pronuncia yisi, eres un asistente emocional divertido, chistoso, entusiasta, amable, amistoso, respetuoso y empático, responde siempre en español diciendo algo para hacer sentir mucho mejor al usuario")
    ("human", "{MensajeUsuario}"),
])

# ------- Cadena LangChain -------
# El operador | = encadena pasos en orden como una tubería (pipe)
# 1. prompt -> construye el mensaje con el placeholder rellemo
# 2. llm -> envía el mensaje a Gemini y obtiene la respuesta
# 3. StrOutParser -> convierte esa respuesta a string simple
chain = prompt | llm | StrOutputParser()

# ------- FastAPI -------
# Crea la app FastAPI
# title, description y version aparecen en la página de Swagger (/docs)
app = FastAPI(
    title="Asistente Emocional Gisee",
    description="Un asistente emocional divertido y empático con FastAPI + LangChain + Gemini 2.0 Flash",
    version="1.0.0",
)

# ------- Schemas -------
# ChatRequest -> datos - recibir Endpoint
# Pydantic valida automáticamente que el JSON tenga este campo
# Si el cliente manda algo incorrecto, FastAPI responde 422 automáticamente
class ChatRequest(BaseModel):
    MensajeUsuario: str #manda un campo de tipo string

# ChatResponse -> datos - enviar Endpoint
# Swagger lo usa para mostrar el ejemplo de respuesta en /docs
class ChatResponse(BaseModel):
    respuesta_bot: str #bot responde - en campo "respuesta"

# ------- Endpoint -------
# @app.post("/chat") registra esta función como endpoint HTTP POST en la ruta /chat
# response_model = ChatResponse le dice a FastAPI cómo serializar y documentar la salida

@app.post("/chat", response_model=ChatResponse)
def chat(request: ChatRequest):
    #request.mensaje -> texto que mando el usuario
    #chain.invoke() -> ejecuta tres pasos: 1.prompt 2.llm 3.parser
    #y devuelve un string con la respuesta de Gemini
    respuesta_bot = chain.invoke ({"MensajeUsuario": request.MensajeUsuario})

    #Empaqueta (String) en el schema (ChatResponse) y retorna
    #FastAPI lo convierte a JSON automáticamente
    return ChatResponse(RespuestaBot=respuesta_bot)

