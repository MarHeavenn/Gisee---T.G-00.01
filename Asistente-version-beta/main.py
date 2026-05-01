# FastAPI=framework web -> crear la API REST - genera el Swagger automáticamente
from fastapi import FastAPI, HTTPException

# BaseModel es de Pydantic — definir la "forma" de los datos que entran y salen del endpoint (como un contrato de datos)
from pydantic import BaseModel

# ChatOpenAI es el wrapper de LangChain compatible con cualquier API estilo OpenAI
# xAI (Grok) usa el mismo formato, por eso reutilizamos este wrapper apuntando a otra URL
from langchain_openai import ChatOpenAI

# ChatPromptTemplate permite construir el prompt con roles:
# "system" (personalidad del bot) y "human" (mensaje del usuario)
from langchain_core.prompts import ChatPromptTemplate

# StrOutputParser convierte la respuesta de Grok (un objeto complejo) => string plano y legible
from langchain_core.output_parsers import StrOutputParser

# load_dotenv lee el archivo .env y carga las variables de entorno
# para que os.getenv() las pueda encontrar en el código
from dotenv import load_dotenv

# os nos da acceso a las variables del sistema operativo
# lo usamos para leer XAI_API_KEY desde el .env
import os

# Ejecuta load_dotenv() para que las variables del .env
# queden disponibles en este proceso
load_dotenv()

# ------- LLM -------

# Le decimos a LangChain qué modelo usar y dónde encontrarlo
# model: nombre exacto del modelo de xAI
# api_key: clave del .env
# base_url: apunta a los servidores de xAI en lugar de OpenAI
llm = ChatOpenAI(
    model="grok-3",
    api_key=os.getenv("GROK_API_KEY"),
    base_url="https://api.x.ai/v1",
)

# ------- Prompt -------
# Definimos la "personalidad" y estructura del chat
# from_messages = recibe una lista de tuplas (rol, contenido)
# "system" -> instrucciones para el bot (personalidad de Gisee)
# "human" -> mensaje del usuario
# {MensajeUsuario} -> placeholder que se rellena en tiempo de ejecución
prompt = ChatPromptTemplate.from_messages([
    ("system", "Eres Gisee, se pronuncia yisi, eres un asistente emocional divertido, chistoso, entusiasta, amable, amistoso, respetuoso y empático, responde siempre en español diciendo algo para hacer sentir mucho mejor al usuario"),
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
    description="Un asistente emocional divertido y empático con FastAPI + LangChain + Grok",
    version="1.0.0",
)

# ------- Schemas -------
# ChatRequest -> datos a recibir en el Endpoint
# Pydantic valida automáticamente que el JSON tenga este campo
# Si el cliente manda algo incorrecto, FastAPI responde 422 automáticamente
class ChatRequest(BaseModel):
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
        # request.MensajeUsuario -> texto que mandó el usuario
        # chain.invoke() -> ejecuta tres pasos: 1.prompt 2.llm 3.parser
        # y devuelve un string con la respuesta de Grok
        respuesta_bot = chain.invoke({"MensajeUsuario": request.MensajeUsuario})

        # Empaqueta el string en el schema ChatResponse y lo retorna
        # FastAPI lo convierte a JSON automáticamente
        return ChatResponse(respuesta_bot=respuesta_bot)

    except Exception as e:
        # Si algo falla (cuota, red, etc.), responde con un mensaje amable
        # en lugar de explotar con un error 500
        raise HTTPException(
            status_code=503,
            detail="Gisee no está disponible en este momento. Intenta más tarde."
        )

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8000)