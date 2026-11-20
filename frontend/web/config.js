// Configuración del front.
//
// `apiBase` apunta al API Gateway: es la única puerta de entrada al sistema,
// así que el front habla con él (nunca directamente con los servicios internos).
// `modoDemo` permite trabajar la interfaz sin el backend levantado: cuando la
// API no responde o el inicio de sesión falla, la pantalla usa datos de
// muestra y lo avisa.
//
// Las credenciales son las del gateway (`backend/gateway/usuarios.py`), que
// publica un juego de demostración: la interfaz entra como **cliente** para
// las rutas del BFF (`/bff/web/**`) y como **admin** para el CRUD del recinto
// (`/api/v1/butacas/**`), que el gateway solo abre con rol admin.
window.TICKETERA = {
  apiBase: 'http://localhost:8080',
  modoDemo: true,
  correoCliente: 'cliente@ticketera.local',
  claveCliente: 'cliente123',
  correoAdmin: 'admin@ticketera.local',
  claveAdmin: 'admin123',
};
