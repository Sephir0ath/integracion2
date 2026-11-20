// =========================================================================
// Pruebas de la autenticación de la interfaz web contra el gateway.
//
// El front se autentica de verdad: pide un token a `POST /api/v1/auth/token`
// y lo manda en la cabecera `Authorization`. Estas pruebas lo comprueban
// sin navegador, con el `fetch` programable del entorno.
//
//   node --test frontend/web/tests/
// =========================================================================
'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');

const { cargarFront } = require('./entorno.js');

const CATALOGO_REAL = {
  eventos: [
    {
      evento_id: 'ev-real',
      nombre: 'Concierto real',
      lugar: 'Teatro real',
      fecha: '2026-11-28T19:30:00+00:00',
    },
  ],
  secciones: [
    {
      seccion_id: 'sec-real',
      evento_id: 'ev-real',
      nombre: 'Tribuna',
      capacidad_total: 2,
      precio_centavos: 55000,
    },
  ],
  metodos_pago: [{ metodo: 'debito', descripcion: 'Débito' }],
};

const BUTACAS_REALES = {
  butacas: [
    { butaca_id: 'sec-real-A1', seccion_id: 'sec-real', fila: 'A', numero: 1, estado: 'LIBRE' },
  ],
};

/** El token que el gateway emite para cada identidad. */
function tokenDe(identidad, numero) {
  return {
    access_token: 'T-' + identidad + '-' + numero,
    token_type: 'bearer',
    expires_in: 3600,
    cliente_id: identidad + '-1',
    role: identidad,
  };
}

/**
 * Un gateway sano: solo acepta las credenciales que el front tiene en
 * `config.js` (si el front mandara otras, la respuesta sería 401).
 *
 * `ajustes.fallarCatalogo` dice cuántas llamadas al catálogo devuelven 401
 * y `ajustes.fallarTodo` hace que toda ruta de negocio conteste 401.
 */
function gatewaySano(config, ajustes) {
  const opciones = ajustes || {};
  const emitidos = { cliente: 0, admin: 0 };
  return async (llamada) => {
    const ruta = llamada.url.replace(/^https?:\/\/[^/]+/, '');
    const sinSesion = { estado: 401, cuerpo: { code: 'NO_AUTENTICADO', message: 'el token ya expiro' } };

    if (ruta === '/api/v1/auth/token') {
      const cuerpo = JSON.parse(llamada.cuerpo);
      let identidad = null;
      if (cuerpo.email === config.correoCliente && cuerpo.password === config.claveCliente) {
        identidad = 'cliente';
      } else if (cuerpo.email === config.correoAdmin && cuerpo.password === config.claveAdmin) {
        identidad = 'admin';
      }
      if (!identidad) {
        return { estado: 401, cuerpo: { code: 'NO_AUTENTICADO', message: 'correo o contrasena incorrectos' } };
      }
      emitidos[identidad] += 1;
      return { estado: 200, cuerpo: tokenDe(identidad, emitidos[identidad]) };
    }

    if (opciones.fallarTodo) return sinSesion;

    if (ruta === '/bff/web/catalogo') {
      if (opciones.fallarCatalogo) {
        opciones.fallarCatalogo -= 1;
        return sinSesion;
      }
      return { estado: 200, cuerpo: CATALOGO_REAL };
    }

    if (/\/bff\/web\/secciones\/.+\/butacas$/.test(ruta)) {
      return { estado: 200, cuerpo: BUTACAS_REALES };
    }

    if (ruta === '/api/v1/butacas/eventos' && llamada.metodo === 'POST') {
      return { estado: 200, cuerpo: { evento: JSON.parse(llamada.cuerpo) } };
    }

    return { estado: 404, cuerpo: { message: 'ruta desconocida: ' + ruta } };
  };
}

/**
 * Carga el front con un gateway de prueba y arranca la interfaz. El
 * programa se crea una sola vez (para que conserve su estado entre
 * llamadas) con la configuración que el front tiene cargada.
 */
async function frontCon(crearPrograma) {
  const ref = { programa: null };
  const front = await cargarFront(async (llamada) => {
    if (!ref.programa) ref.programa = crearPrograma(ref.config);
    return ref.programa(llamada);
  });
  ref.config = front.config;
  await front.iniciar();
  return front;
}

/** Rellena y envía el formulario de evento de la vista de administración. */
async function enviarEvento(front, nombre) {
  const formulario = front.documento.querySelector('#formulario-evento');
  formulario.nombre.value = nombre;
  formulario.lugar.value = 'Teatro';
  formulario.fecha.value = '2026-12-01T20:00';
  await formulario.dispatch('submit', {
    type: 'submit',
    target: formulario,
    preventDefault() {},
  });
  await front.asentar();
}

test('pide el token de cliente antes de la primera llamada de la API y lo manda en Authorization', async () => {
  const front = await frontCon((config) => gatewaySano(config));

  const tokens = front.peticiones('/api/v1/auth/token');
  assert.equal(tokens.length, 1, 'el token se pide una sola vez mientras siga vigente');
  assert.deepEqual(JSON.parse(tokens[0].cuerpo), {
    email: front.config.correoCliente,
    password: front.config.claveCliente,
  });

  const catalogo = front.peticiones('/bff/web/catalogo');
  assert.equal(catalogo.length, 1);
  assert.equal(catalogo[0].cabeceras.Authorization, 'Bearer T-cliente-1');
  assert.equal(front.app.estado.modoDemo, false, 'con la API viva no se cae al modo demo');
});

test('el CRUD del recinto usa el token de admin, no el de cliente', async () => {
  const front = await frontCon((config) => gatewaySano(config));

  await enviarEvento(front, 'Concierto nuevo');

  const altas = front.peticiones('/api/v1/butacas/eventos').filter((c) => c.metodo === 'POST');
  assert.equal(altas.length, 1, 'se creó el evento por el gateway');
  assert.equal(altas[0].cabeceras.Authorization, 'Bearer T-admin-1');

  const tokens = front.peticiones('/api/v1/auth/token');
  assert.equal(tokens.length, 2, 'un token por identidad');
  assert.deepEqual(JSON.parse(tokens[1].cuerpo), {
    email: front.config.correoAdmin,
    password: front.config.claveAdmin,
  });
});

test('un 401 vuelve a pedir el token y reintenta una sola vez', async () => {
  const front = await frontCon((config) => gatewaySano(config, { fallarCatalogo: 1 }));

  assert.equal(front.peticiones('/api/v1/auth/token').length, 2, 'se pidió un token nuevo');
  const catalogos = front.peticiones('/bff/web/catalogo');
  assert.equal(catalogos.length, 2, 'el catálogo se intentó una vez más');
  assert.equal(catalogos[0].cabeceras.Authorization, 'Bearer T-cliente-1');
  assert.equal(catalogos[1].cabeceras.Authorization, 'Bearer T-cliente-2');
  assert.equal(front.app.estado.modoDemo, false, 'un 401 no es "el backend está caído"');
});

test('un 401 que se repite no se reintenta más de una vez', async () => {
  const front = await frontCon((config) => gatewaySano(config, { fallarTodo: true }));

  assert.equal(front.peticiones('/bff/web/catalogo').length, 2, 'dos intentos, ni uno más');
  assert.equal(front.app.estado.modoDemo, true, 'sin sesión válida se cae al modo demo');
  assert.equal(front.app.estado.motivoDemo, 'no-autorizado');
});

test('si el login falla, el front entra en modo demo con los datos de muestra', async () => {
  const front = await frontCon(() => async () => ({
    estado: 401,
    cuerpo: { code: 'NO_AUTENTICADO', message: 'correo o contrasena incorrectos' },
  }));

  assert.equal(front.app.estado.modoDemo, true);
  assert.equal(front.app.estado.motivoDemo, 'login');
  assert.deepEqual(
    front.app.estado.eventos.map((e) => e.evento_id),
    front.fixtures.eventos.map((e) => e.evento_id),
    'se muestran los datos de muestra'
  );
  assert.equal(
    front.documento.querySelector('#aviso').textContent.indexOf('demostración') !== -1,
    true
  );
});

test('si la API no responde, el front entra en modo demo (comportamiento anterior)', async () => {
  const front = await frontCon(() => async () => new Error('connection refused'));

  assert.equal(front.app.estado.modoDemo, true);
  assert.equal(front.app.estado.motivoDemo, 'sin-respuesta');
  assert.equal(front.app.estado.eventos.length, front.fixtures.eventos.length);
  assert.equal(
    front.documento.querySelector('#aviso').textContent,
    'Sin respuesta de la API: se muestran datos de demostración.'
  );
});

test('en modo demo la compra se simula y no se llama a la API', async () => {
  const front = await frontCon(() => async () => new Error('connection refused'));
  const llamadasAntes = front.llamadas.length;

  const resultado = await front.app.repo.comprar({
    saga_id: 'saga-demo',
    evento_id: 'ev-1',
    seccion_id: 'sec-1',
    butaca_ids: ['sec-1-A1'],
  });

  assert.equal(resultado.simulado, true);
  assert.equal(resultado.compra.estado, 'CONFIRMADA');
  assert.equal(front.llamadas.length, llamadasAntes, 'la compra simulada no toca la API');
});

test('cerrar la sesión borra los dos tokens y el siguiente pedido vuelve a pedir credenciales', async () => {
  const front = await frontCon((config) => gatewaySano(config));

  await enviarEvento(front, 'Concierto nuevo');
  await front.documento.querySelector('#salir').dispatch('click');

  assert.equal(front.app.estado.token, null);
  assert.equal(front.app.estado.tokenAdmin, null);

  await front.app.repo.catalogo();
  assert.equal(front.peticiones('/api/v1/auth/token').length, 3, 'el cliente y el admin vuelven a pedir token');
});
