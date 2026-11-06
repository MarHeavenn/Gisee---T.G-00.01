import re

def limpiar_texto_para_tts(texto: str) -> str:
    """
    Limpia el texto antes de enviarlo al TTS para que suene natural.
    """
    
    # 1. quita enlaces markdown: [texto](url) -> texto (no leer URLs)
    texto = re.sub(r'\[([^\]]*)\]\([^)]*\)', r'\1', texto)

    # elimina emojis usando rangos unicode específicos
    emoji_pattern = re.compile(
        "["
        "\U0001F600-\U0001F64F"  # emojis de caras
        "\U0001F300-\U0001F5FF"  # símbolos y pictogramas
        "\U0001F680-\U0001F6FF"  # transporte y mapas
        "\U0001F1E0-\U0001F1FF"  # banderas
        "\U00002702-\U000027B0"  # símbolos varios
        "\U000024C2-\U0001F251"  # símbolos adicionales
        "]+",
        flags=re.UNICODE
    )
    texto = emoji_pattern.sub('', texto)
    
    # 2. cuando se refiere al USUARIO (ti mismo/a, tú mismo/a)
    # Gisee es un asistente masculino: usa las formas en -o
    texto = re.sub(r'\bti mismo/a\b', 'ti mismo', texto, flags=re.IGNORECASE)
    texto = re.sub(r'\btú mismo/a\b', 'tú mismo', texto, flags=re.IGNORECASE)
    texto = re.sub(r'\bmismo/a\b', 'mismo', texto, flags=re.IGNORECASE)
    texto = re.sub(r'\bél/ella\b', 'él', texto, flags=re.IGNORECASE)

    # 3. cuando se refiere a OTRA PERSONA (amigo/a, compañero/a, etc.)
    # lo expande a "amigo o amiga" para que suene natural al leerlo
    texto = re.sub(r'\bamigo/a\b', 'amigo o amiga', texto, flags=re.IGNORECASE)
    texto = re.sub(r'\bcompañero/a\b', 'compañero o compañera', texto, flags=re.IGNORECASE)
    texto = re.sub(r'\bquerido/a\b', 'querido', texto, flags=re.IGNORECASE)
    texto = re.sub(r'\bchico/a\b', 'chico o chica', texto, flags=re.IGNORECASE)
    texto = re.sub(r'\bniño/a\b', 'niño o niña', texto, flags=re.IGNORECASE)
    texto = re.sub(r'\bnovio/a\b', 'novio o novia', texto, flags=re.IGNORECASE)
    texto = re.sub(r'\bhijo/a\b', 'hijo o hija', texto, flags=re.IGNORECASE)

    # 4. patrón genérico para cualquier palabra/a que no hayamos cubierto.
    # El TTS no debe leer la barra: nos quedamos con la forma sin "/a".
    texto = re.sub(r'(\w+)/a\b', r'\1', texto)
    
    # 5. elimina asteriscos de markdown
    texto = re.sub(r'\*+', '', texto)
    
    # 6. reemplaza barras que queden por pausa
    texto = re.sub(r'/+', ', ', texto)
    
    # 7. elimina guiones bajos de markdown
    texto = re.sub(r'_+', '', texto)
    
    # 8. elimina almohadillas de markdown
    texto = re.sub(r'#+\s*', '', texto)
    
    # 9. agrega signo de apertura a preguntas que no lo tienen
    def arreglar_pregunta(match):
        frase = match.group(0)
        if not frase.startswith('¿'):
            return '¿' + frase
        return frase
    texto = re.sub(r'¿?[^.!?\n]*\?', arreglar_pregunta, texto)
    
    # 10. limpia espacios múltiples y saltos de línea
    texto = re.sub(r'\n+', ' ', texto)
    texto = re.sub(r' +', ' ', texto)

    # 11. Reemplazo fonético para que el TTS pronuncie bien su nombre
    texto = re.sub(r'\bGisee\b', 'Yísi', texto, flags=re.IGNORECASE)
    
    return texto.strip()