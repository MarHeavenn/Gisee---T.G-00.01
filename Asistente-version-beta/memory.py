# Base de datos vectorial para contexto y memoria conversacional con ChromaDB
from langchain_chroma import Chroma

# Document -> objeto para guardar texto
from langchain_core.documents import Document

# importo embeddings ya configurados en el rag (evitar descargar modelo dos veces)
from rag import embeddings

import os

# Carpeta para guardar la memoria de conversación
CarpetaChromaMemoria = "./memory"

def guardar_en_memoria(session_id: str, mensaje: str):
    """
    Guarda mensaje -> ChromaDB (memoria vectorial conversacional)
    """
    from langchain_core.documents import Document

    vectorstore = Chroma(
        persist_directory=CarpetaChromaMemoria,
        embedding_function=embeddings,
        collection_name=f"memoria_{session_id}",
    )

    # guarda el mensaje como un documento con metadato de sesión
    vectorstore.add_documents([Document(page_content=mensaje, metadata={"session_id": session_id})])

def buscar_en_memoria(session_id: str, pregunta: str) -> str:
    """
    Busca en la memoria de esta sesión los mensajes anteriores
    más relevantes para la pregunta actual.
    Devuelve un string con esos mensajes para pasárselo al LLM.
    """
    # si no existe la carpeta de memoria aún, no hay nada que buscar
    if not os.path.exists(CarpetaChromaMemoria):
        return ""

    try:
        vectorstore = Chroma(
            persist_directory=CarpetaChromaMemoria,
            embedding_function=embeddings,
            collection_name=f"memoria_{session_id}",
        )

        # busca los 3 mensajes anteriores más relevantes
        resultados = vectorstore.similarity_search(pregunta, k=3)

        # une los mensajes encontrados en un solo texto
        return "\n".join([r.page_content for r in resultados])
    except:
        # si la colección no existe todavía, retorna vacío
        return ""