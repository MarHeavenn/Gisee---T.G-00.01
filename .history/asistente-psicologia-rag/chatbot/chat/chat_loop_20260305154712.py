from chatbot.core.chat_chain import handle_chat_flow
from chatbot.core.redis_client import delete_session
from chatbot.core.tts_engine import speak
from chatbot.db.user_repository import UserRepository


def chat_loop(user_id, name):
    
    # 1. extraer información del usuario de la base de datos
    user_context = UserRepository().get_by_id(user_id).context
    
    speak(f"Hola {name}, ya estás autenticado. ¿En qué puedo ayudarte hoy?")

    while True:
        # --------- WHISPER DESACTIVADO PARA PRUEBAS ---------
        # audio = record_audio(duration=5)
        # user_input = transcribe_audio(audio).lower()
        
        # --------- entrada de texto manual ---------
        user_input = input("Escribe tu pregunta -> ").lower()
        
        print(f"Tú dijiste: {user_input}")

        if any(
            p in user_input for p in ["cerrar sesión", "salir", "adiós", "hasta luego"]
        ):
            speak("Sesión cerrada correctamente. ¡Hasta pronto!")
            delete_session()
            break

        try:
            # --------- uso de gemini con intervención y contexto ---------
            # response = model.generate_content(user_input, user_context)
            # ai_text = response.text.strip()
            ai_text, user_new_context = handle_chat_flow(user_input, user_context)
            
            # 4. almacenar la información en la base de datos ( como no sabemos cuando el usuario va a terminar la conversación, almacenamos cada que se procese)
            UserRepository().update(user_id, context=user_new_context)
            user_context = user_new_context
            
            # --------- uso de gemini sin intervención ni contexto ---------
            # response = model.generate_content(user_input)
            # ai_text = response.text.strip()
            
            speak(ai_text)
            print(f"IA: {ai_text}")
        except Exception as e:
            print(f"Error con Gemini: {e}")
            speak("Lo siento, tuve un problema para responder.")
