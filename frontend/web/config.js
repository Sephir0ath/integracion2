// Configuración del front.
//
// `apiBase` apunta al API Gateway: es la única puerta de entrada al sistema,
// así que el front habla con él (nunca directamente con los servicios internos).
// `modoDemo` permite trabajar la interfaz sin el backend levantado: cuando la
// API no responde, la pantalla usa datos de muestra y lo avisa.
window.TICKETERA = {
  apiBase: 'http://localhost:8080',
  modoDemo: true,
  correoDemo: 'comprador@ticketera.cl',
  claveDemo: 'ticketera123',
  claveAdmin: 'admin-ticketera',
};
