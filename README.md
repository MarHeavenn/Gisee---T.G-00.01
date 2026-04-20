# Gisee---T.G-00.01
La construcción de Gisee incluira una bocina y un microfono para hablar con el usuario (TTS), tendrá una cabeza de mascota para generar familiaridad, dos oled para tener ojos que parpadean, llantas para el desplazamiento, una cola de mascota y una I.A integrada para manejar una interacción de prevención y desarrollo de crisis depresivo lo más amena posible para el usuario.

La funcionalidad de Gisee es ser una mascota amistosa la cual mitigue la soledad, combata la depresión y beneficie al usuario emocionalmente.

# Ramas
- main          ← código estable / producción
- develop       ← integración general
- feature/software-vscode
- feature/hardware-arduino

# Instrucciones
1. uvicorn main:app --reload
- Si dice "Application startup complete." significa que el servidor arranco correctamente
2. Pegar en el navegador http://127.0.0.1:8000/docs
- Ahí verás el Swagger UI — la interfaz interactiva donde puedes chatear con Gisee
3. Haz clic en POST /chat → "Try it out" → escribe tu mensaje → "Execute".
- Reemplaza el texto en el cuadro donde dice "string" con tu mensaje real (EJEMPLO: "MensajeUsuario": "hola, me siento triste hoy")
