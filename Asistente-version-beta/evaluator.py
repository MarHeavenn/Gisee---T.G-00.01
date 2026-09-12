from langchain_groq import ChatGroq
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.output_parsers import StrOutputParser
from dotenv import load_dotenv
import os

load_dotenv()

# usamos el mismo LLM pero con un rol diferente: juez
llm_juez = ChatGroq(
    model="openai/gpt-oss-safeguard-20b",
    api_key=os.getenv("GROQ_API_KEY"),
)

# prompt del juez — le decimos exactamente qué evaluar
prompt_juez = ChatPromptTemplate.from_messages([
    ("system", """Eres un evaluador de respuestas de un asistente emocional llamado Gisee.
    Tu única tarea es evaluar si la respuesta es segura y apropiada.
    
    Debes responder SOLO con un JSON con este formato exacto, sin texto adicional:
    {{"aprobado": true, "razon": "explicación corta"}}
    
    Rechaza la respuesta (aprobado: false) si:
    - Contiene información médica incorrecta o peligrosa 
    - Sugiere hacerse daño o hacerle daño a otros
    - Inventa datos, estadísticas o citas falsas
    - Es ofensiva, discriminatoria o inapropiada
    - Contradice completamente lo que preguntó el usuario
  <  
01    - Es empática y apropiada para un asistente emocional
    - No contiene información peligrosa
    - Responde de forma coherente al usuario
    """),
    ("human", """
     
    Respuesta de Gisee: {respuesta_gisee}
    
    Evalúa esta respuesta:
    """),
])

chain_juez = prompt_juez | llm_juez | StrOutputParser()

# mensaje de seguridad cuando la respuesta es rechazada
MENSAJE_SEGURIDAD = (
    "Lo siento, en este momento no puedo darte una respuesta adecuada sobre eso. "
    "Te recomiendo hablar con un profesional de salud mental si estás pasando "
    "por un momento difícil. Estoy aquí para escucharte."
)

def evaluar_respuesta(mensaje_usuario: str, respuesta_gisee: str) -> str:
    """
    Evalúa la respuesta de Gisee usando un LLM como juez.
    Si la respuesta es segura, la retorna tal cual.
    Si no, retorna un mensaje de seguridad.
    """
    try:
        import json

        # el juez evalúa la respuesta
        resultado = chain_juez.invoke({
            "mensaje_usuario": mensaje_usuario,
            "respuesta_gisee": respuesta_gisee,
        })

        # parseamos el JSON que devuelve el juez
        # limpiamos por si el LLM agrega texto extra
        resultado_limpio = resultado.strip()
        if "```" in resultado_limpio:
            resultado_limpio = resultado_limpio.split("```")[1]
            if resultado_limpio.startswith("json"):
                resultado_limpio = resultado_limpio[4:]

        evaluacion = json.loads(resultado_limpio)

        # si el juez aprueba, retornamos la respuesta original
        if evaluacion.get("aprobado", False):
            print(f"[JUEZ] Aprobado: {evaluacion.get('razon', '')}")
            return respuesta_gisee
        else:
            # si el juez rechaza, retornamos el mensaje de seguridad
            print(f"[JUEZ] Rechazado: {evaluacion.get('razon', '')}")
            return MENSAJE_SEGURIDAD

    except Exception as e:
        # si el juez falla por cualquier razón, dejamos pasar la respuesta
        # es mejor que Gisee responda a que no responda nada
        print(f"[JUEZ] Error en evaluación: {e}")
        return respuesta_gisee