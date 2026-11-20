/**
 * Utilidades compartidas por las pruebas end-to-end.
 *
 * La suite habla con el sistema de dos maneras:
 *
 *   - **por la interfaz** (lo que ve la persona), que es el objeto de la
 *     prueba end-to-end;
 *   - **por la API** (gateway en :8080) solo para **preparar** y
 *     **verificar**: crear un evento con butacas libres antes de la prueba
 *     y consultar despues lo que quedo persistido.
 *
 * El sistema de docker tiene que estar arriba:
 *
 *   docker compose up -d --build
 */

const { expect } = require('@playwright/test');
const { execFileSync } = require('node:child_process');
const path = require('node:path');

const API = process.env.E2E_API_BASE || 'http://127.0.0.1:8080';
const UI = process.env.E2E_UI_BASE || 'http://127.0.0.1:8081';
const RAIZ = path.resolve(__dirname, '..', '..');

/** Usuarios de demostracion que publica el gateway (`backend/gateway/usuarios.py`). */
const CREDENCIALES = {
  cliente: { email: 'cliente@ticketera.local', password: 'cliente123' },
  admin: { email: 'admin@ticketera.local', password: 'admin123' },
};

/**
 * Peticion a la API que respeta el limite del gateway.
 *
 * El gateway limita a 60 peticiones por minuto y cliente (burst 20) y
 * responde 429 con la cabecera `Retry-After`: sin esperarla, una suite
 * larga se expulsaria a si misma. Aqui se espera lo que dice el gateway y
 * se reintenta.
 *
 * @param {import('@playwright/test').APIRequestContext} request
 * @param {'get'|'post'|'put'|'delete'} metodo
 * @param {string} ruta ruta del gateway, con la barra inicial
 * @param {object} [opciones] `data` y `headers` de la peticion
 */
async function pedirApi(request, metodo, ruta, opciones = {}) {
  const INTENTOS = 6;
  let respuesta = null;
  for (let intento = 1; intento <= INTENTOS; intento += 1) {
    respuesta = await request[metodo](`${API}${ruta}`, opciones);
    if (respuesta.status() !== 429) return respuesta;
    const espera = Number(respuesta.headers()['retry-after']) || 5;
    await new Promise((resolver) => setTimeout(resolver, Math.min(espera, 30) * 1000));
  }
  return respuesta;
}

/**
 * Token del gateway para una identidad (`cliente` o `admin`).
 *
 * @param {import('@playwright/test').APIRequestContext} request
 * @param {'cliente'|'admin'} identidad
 * @returns {Promise<string>} el `access_token`
 */
async function tokenDe(request, identidad = 'cliente') {
  const respuesta = await pedirApi(request, 'post', '/api/v1/auth/token', {
    data: CREDENCIALES[identidad],
  });
  expect(respuesta.status(), `el gateway no abrio sesion para ${identidad}`).toBe(200);
  const cuerpo = await respuesta.json();
  return cuerpo.access_token;
}

/**
 * Crea un evento con una seccion de butacas libres, por la API de
 * administracion del gateway (no por la interfaz: aqui interesa el dato,
 * no la pantalla).
 *
 * @returns {Promise<{evento: object, seccion: object, token: string}>}
 */
async function crearEventoConSeccion(request, opciones = {}) {
  const {
    nombre = `E2E ${new Date().toISOString().slice(11, 19)}`,
    capacidad = 8,
    precio = 10000,
  } = opciones;
  const token = await tokenDe(request, 'admin');

  let evento = null;
  try {
    const respuestaEvento = await pedirApi(request, 'post', '/api/v1/butacas/eventos', {
      headers: { Authorization: `Bearer ${token}` },
      data: { nombre, lugar: 'Sala de pruebas E2E', fecha: '2027-03-15T20:00:00Z' },
    });
    expect(respuestaEvento.status(), 'no se pudo crear el evento').toBe(200);
    evento = (await respuestaEvento.json()).evento;

    const respuestaSeccion = await pedirApi(request, 'post', '/api/v1/butacas/secciones', {
      headers: { Authorization: `Bearer ${token}` },
      data: {
        evento_id: evento.evento_id,
        nombre: 'Platea',
        capacidad_total: capacidad,
        precio_centavos: precio,
      },
    });
    expect(respuestaSeccion.status(), 'no se pudo crear la seccion').toBe(200);
    const seccion = (await respuestaSeccion.json()).seccion;

    return { evento, seccion, token };
  } catch (fallo) {
    // Si fallo la seccion (por ejemplo con un 429 del gateway), el evento ya
    // quedo creado: se borra para no dejar basura en el catalogo.
    if (evento) await borrarEvento(request, evento.evento_id, token);
    throw fallo;
  }
}

/**
 * Borra el evento de la prueba (y con el sus secciones y butacas).
 *
 * El dominio **no** deja borrar un evento con butacas reservadas o vendidas
 * (una venta deja rastro), asi que cuando la API rechaza el borrado la
 * limpieza se hace directo en la base: es limpieza de fixture, no parte del
 * sistema que se esta probando.
 *
 * @param {import('@playwright/test').APIRequestContext} request
 * @param {string} eventoId
 * @param {string} [token] token de admin, si ya se tiene
 * @returns {Promise<'api'|'base'>} como se limpio
 */
async function borrarEvento(request, eventoId, token) {
  const propio = token || (await tokenDe(request, 'admin'));
  const respuesta = await pedirApi(request, 'delete', `/api/v1/butacas/eventos/${encodeURIComponent(eventoId)}`, {
    headers: { Authorization: `Bearer ${propio}` },
  });
  if (respuesta.ok()) return 'api';
  limpiarEventoEnLaBase(eventoId);
  return 'base';
}

/**
 * Limpieza directa en la base de `butacas` (por el orden de las claves
 * foraneas: butacas -> secciones -> evento).
 */
function limpiarEventoEnLaBase(eventoId) {
  const identificador = String(eventoId).replace(/[^A-Za-z0-9_-]/g, '');
  if (!identificador) throw new Error(`identificador de evento invalido: ${eventoId}`);
  const sql = [
    `delete from butaca where seccion_id in (select seccion_id from seccion where evento_id = '${identificador}')`,
    `delete from seccion where evento_id = '${identificador}'`,
    `delete from evento where evento_id = '${identificador}'`,
  ].join('; ');
  execFileSync(
    'docker',
    ['compose', 'exec', '-T', 'butacas_db', 'psql', '-U', 'app', '-d', 'butacas', '-c', sql],
    { cwd: RAIZ, stdio: 'ignore' },
  );
}

/**
 * Espera a que la interfaz haya cargado el catalogo real y haya abierto la
 * sesion por su cuenta (la UI pide el token sola: no hay pantalla de login).
 */
async function esperarCatalogo(page) {
  await expect(page.locator('#programa li button').first()).toBeVisible({ timeout: 30_000 });
  await expect(page.locator('#sesion')).toHaveText(/@/, { timeout: 30_000 });
  // El modo demo se anuncia con un aviso: si aparece, la UI no hablo con el
  // sistema real y la prueba no tiene sentido. El elemento #aviso siempre
  // esta en el DOM; lo que cambia es que este oculto.
  await expect(page.locator('#aviso')).toBeHidden();
}

/**
 * Elige un evento del programa por su nombre.
 */
async function elegirEvento(page, nombre) {
  const boton = page.locator('#programa li button', { hasText: nombre }).first();
  await boton.click();
  await expect(page.locator('#plano button.butaca').first()).toBeVisible({ timeout: 30_000 });
}

/**
 * Elige las primeras butacas libres del plano.
 *
 * @returns {Promise<string[]>} las etiquetas (`Fila A, butaca 3, libre`)
 */
async function elegirButacas(page, cantidad = 2) {
  const libres = page.locator('#plano button.butaca:not([disabled])');
  const total = await libres.count();
  expect(total, 'el plano no tiene butacas libres').toBeGreaterThanOrEqual(cantidad);
  const elegidas = [];
  for (let indice = 0; indice < cantidad; indice += 1) {
    const boton = libres.nth(indice);
    elegidas.push(await boton.getAttribute('aria-label'));
    await boton.click();
  }
  return elegidas;
}

/**
 * Compra lo que hay en el carrito y devuelve el identificador de la saga.
 *
 * @returns {Promise<string>} el `saga_id`
 */
async function comprar(page) {
  await page.locator('#carrito button', { hasText: 'Comprar' }).click();
  await expect(page.locator('#entradas')).toContainText(/CONFIRMADA/, { timeout: 60_000 });
  const saga = await page.locator('#entradas span[title]').first().getAttribute('title');
  expect(saga, 'la compra no mostro el identificador de la saga').toBeTruthy();
  return saga;
}

/**
 * Vigila la consola, las peticiones fallidas y las respuestas 5xx de la
 * pagina. Devuelve el arreglo donde se acumulan, para poder afirmar que la
 * interfaz no dejo errores.
 */
function vigilar(page) {
  const problemas = [];
  page.on('console', (mensaje) => {
    if (mensaje.type() === 'error') problemas.push(`consola: ${mensaje.text()}`);
  });
  page.on('pageerror', (error) => {
    problemas.push(`excepcion: ${error.message}`);
  });
  page.on('requestfailed', (peticion) => {
    const fallo = peticion.failure();
    problemas.push(`peticion: ${peticion.method()} ${peticion.url()} (${fallo ? fallo.errorText : 'sin motivo'})`);
  });
  page.on('response', (respuesta) => {
    if (respuesta.status() >= 500) problemas.push(`respuesta: ${respuesta.status()} ${respuesta.url()}`);
  });
  return problemas;
}

module.exports = {
  API,
  UI,
  CREDENCIALES,
  pedirApi,
  tokenDe,
  crearEventoConSeccion,
  borrarEvento,
  esperarCatalogo,
  elegirEvento,
  elegirButacas,
  comprar,
  vigilar,
};
