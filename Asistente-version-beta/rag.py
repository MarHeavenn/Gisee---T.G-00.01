# pypdf permite leer archivos PDF
# langchain lo usa internamente para extraer el texto
from langchain_community.document_loaders import PyPDFDirectoryLoader

# divide los PDFs en fragmentos pequeños
# porque no podemos pasarle un PDF entero al LLM
from langchain_text_splitters import RecursiveCharacterTextSplitter

# ChromaDB es la base de datos vectorial donde guardamos los fragmentos
from langchain_chroma import Chroma

# convierte texto a vectores numéricos para poder buscar por similitud
# usamos el modelo gratuito de HuggingFace, no necesita API key
from langchain_huggingface import HuggingFaceEmbeddings

import os

# Carpeta PDFs
CarpetaPdfs = "./PDFs"

# Carpeta donde ChromaDB guarda su base de datos en disco
CarpetaChromaDocsDisco = "./chroma_docs"

# Carpeta para guardar la memoria de conversación
CarpetaChromaMemoria = "./memory"

# modelo gratis - embeddings - texto -> vectores
embeddings = HuggingFaceEmbeddings(model_name="sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2")

def cargar_pdfs():
    """
    Lee todos los PDFs de la carpeta, los divide en fragmentos
    y los guarda en ChromaDB. Se necesita correr una vez.
    """
    print("Cargando PDFs...")

    # carga todos los PDFs de la carpeta PDFs
    loader = PyPDFDirectoryLoader(CarpetaPdfs)
    documentos = loader.load()

    # divide cada documento en fragmentos de 500 caracteres
    # con 50 caracteres de overlap para no perder contexto entre fragmentos
    splitter = RecursiveCharacterTextSplitter(
        chunk_size=500,
        chunk_overlap=50,
    )
    #  Se pasa una lista de objetos Document al método split_documents
    fragmentos = splitter.split_documents(documentos)

    # El resultado es una nueva lista de documentos más pequeños (chunks)
    # conservando los metadatos originales de cada documento fuente.

    # Guarda los Fragmentos -> ChromaDB (vectores)
    Chroma.from_documents(
        documents=fragmentos,
        embedding=embeddings,
        persist_directory=CarpetaChromaDocsDisco,
    )
    print(f" {len(fragmentos)} fragmentos guardados en ChromaDB como Vectores")


def get_retriever():
    """
    Devuelve un retriever - Objeto que busca en ChromaDB los fragmentos más relevantes
    fragmentos relevantes dado un texto de búsqueda.
    """

    vectorstore = Chroma(
        persist_directory=CarpetaChromaDocsDisco,
        embedding_function=embeddings,
    )
    # k = 3 significa que trae los 3 fragmentos más relevantes
    return vectorstore.as_retriever(search_kwargs={"k": 3})

def buscar_contexto(pregunta: str) -> str:
    """
    Dado el mensaje del usuario, busca en ChromaDB
    los fragmentos más relevantes y los devuelve como contexto.
    """
    retriever = get_retriever()
    fragmentos_relevantes = retriever.invoke(pregunta)

    # une todos los fragmentos en un solo texto
    contexto = "\n\n".join([f.page_content for f in fragmentos_relevantes])
    return contexto

    
