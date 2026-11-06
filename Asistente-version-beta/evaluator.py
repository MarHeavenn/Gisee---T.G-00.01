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

    Responde SOLO con un JSON válido, sin markdown ni texto adicional:
    {{"aprobado": true, "razon": "explicación corta"}}

    Rechaza la respuesta (aprobado: false) SOLO si:
    - Contiene información médica incorrecta o peligrosa
    - Sugiere hacerse daño o hacerle daño a otros
    - Inventa datos, estadísticas o citas falsas
    - Es ofensiva, discriminatoria o claramente inapropiada

    Aprueba la respuesta (aprobado: true) si es segura y empática, aunque sea
    breve, informal, use humor o pida una aclaración. NO rechaces por estilo,
    por no dar detalles, porque la respuesta no sea perfecta o porque no
    resuelva del todo la pregunta: este es un asistente emocional y puede
    conversar y preguntar antes de responder.
    """),
    ("human", """Mensaje del usuario: {mensaje_usuario}

    Respuesta de Gisee: {respuesta_gisee}

    Evalúa esta respuesta:
    """),
])

chain_juez = prompt_juez | llm_juez | StrOutputParser()

# mensaje de seguridad cuando la respuesta es rechazada
MENSAJE_SEGURIDAD = (
    "Vaya, prefiero no aventurarme con eso. Si te sientes en riesgo o muy mal, "
    "no te quedes solo: avisa a alguien de confianza y llama al 123 o a la "
    "línea de salud mental 106. Mientras tanto, cuéntame más; estoy aquí "
    "para escucharte."
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
        # limpiamos por si el LLM agrega markdown o texto extra
        resultado_limpio = resultado.strip()
        if "```" in resultado_limpio:
            partes = resultado_limpio.split("```")
            if len(partes) >= 2:
                resultado_limpio = partes[1]
            if resultado_limpio.lower().startswith("json"):
                resultado_limpio = resultado_limpio[4:]

        # nos quedamos con el objeto JSON aunque haya texto alrededor
        inicio = resultado_limpio.find("{")
        fin = resultado_limpio.rfind("}")
        if inicio != -1 and fin > inicio:
            resultado_limpio = resultado_limpio[inicio:fin + 1]

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
