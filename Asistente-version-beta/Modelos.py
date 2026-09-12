# check_modelos.py
# Script rápido para consultar, directo desde la API de Groq,
# qué modelos están disponibles con tu API key en este momento.

from groq import Groq       # cliente oficial de Groq (no el wrapper de LangChain)
from dotenv import load_dotenv  # para leer el .env
import os

# Cargamos las variables del .env (ahí debe estar GROQ_API_KEY)
load_dotenv()

# Creamos el cliente de Groq usando la key del .env
client = Groq(api_key=os.getenv("GROQ_API_KEY"))

# client.models.list() le pregunta directamente a Groq:
# "oye, ¿qué modelos puedo usar con esta key?"
modelos = client.models.list()

# Recorremos la lista y imprimimos cada id de modelo disponible
print("Modelos disponibles en tu cuenta de Groq:")
for m in modelos.data:
    print("-", m.id)