// Datos de muestra para trabajar la interfaz sin el backend.
//
// El plano de butacas se genera de forma determinista: la misma sección
// siempre dibuja la misma sala, así el diseño no cambia entre recargas.
window.FIXTURES = (function () {
  const FILAS = ['A', 'B', 'C', 'D', 'E', 'F'];
  const BUTACAS_POR_FILA = 8;

  const eventos = [
    {
      evento_id: 'ev-1',
      nombre: 'Cielo roto en vivo',
      lugar: 'Sala Diego Portales',
      fecha: '2026-03-14T20:00:00',
    },
    {
      evento_id: 'ev-2',
      nombre: 'Nocturno de piano',
      lugar: 'Sala Norte',
      fecha: '2026-03-21T19:30:00',
    },
    {
      evento_id: 'ev-3',
      nombre: 'Festival de títeres del Sur',
      lugar: 'Teatro Galpón',
      fecha: '2026-03-28T18:00:00',
    },
  ];

  const secciones = [
    { seccion_id: 'sec-1', evento_id: 'ev-1', nombre: 'Platea', capacidad_total: 48, precio_centavos: 2500000 },
    { seccion_id: 'sec-2', evento_id: 'ev-1', nombre: 'Balcón', capacidad_total: 32, precio_centavos: 1500000 },
    { seccion_id: 'sec-3', evento_id: 'ev-2', nombre: 'Parquet', capacidad_total: 40, precio_centavos: 1200000 },
    { seccion_id: 'sec-4', evento_id: 'ev-3', nombre: 'Butacas', capacidad_total: 24, precio_centavos: 800000 },
  ];

  const metodos_pago = [
    { metodo: 'debito', descripcion: 'Débito' },
    { metodo: 'credito', descripcion: 'Crédito' },
    { metodo: 'transferencia', descripcion: 'Transferencia' },
  ];

  function generarButacas(seccion) {
    const butacas = [];
    let numero = 1;
    for (const fila of FILAS) {
      for (let i = 0; i < BUTACAS_POR_FILA; i += 1) {
        if (numero > seccion.capacidad_total) break;
        // Ocupación determinista: se Occupada cada butaca cuya posición cae en
        // el patrón, para que el plano no se vea vacío ni lleno.
        const ocupada = (numero * 7 + fila.charCodeAt(0)) % 11 === 0;
        butacas.push({
          butaca_id: `${seccion.seccion_id}-${fila}${numero}`,
          seccion_id: seccion.seccion_id,
          fila,
          numero,
          estado: ocupada ? 'RESERVADA' : 'LIBRE',
        });
        numero += 1;
      }
    }
    return butacas;
  }

  const butacas = {};
  for (const seccion of secciones) {
    butacas[seccion.seccion_id] = generarButacas(seccion);
  }

  return { eventos, secciones, butacas, metodos_pago };
})();
