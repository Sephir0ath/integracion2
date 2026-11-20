/**
 * Resiliencia y degradacion de la interfaz.
 *
 * Que pasa cuando el sistema no puede responder como se espera: el gateway
 * caido, el login rechazado, un 401 que obliga a renovar la sesion y un
 * catalogo que llega degradado. En cada caso se afirma **lo que ve la
 * persona**: el aviso que aparece en pantalla, los datos que se muestran y
 * lo que la interfaz deja o deja de hacer.
 *
 * Las fallas se provocan en el borde del navegador (`page.route`): se
 * interceptan las peticiones que la interfaz hace al gateway y se les
 * responde con un 401/503 sembrado, o se abortan. El resto del sistema
 * (la base, la saga) queda intacto: la prueba no toca contenedores ni
 * reinicia servicios, y tampoco deja datos (no crea eventos ni ejecuta
 * compras de verdad).
 *
 * Determinismo: cada prueba arranca desde la pagina en blanco, no espera
 * con `sleep` (las aserciones de Playwright reintentan solas) y no depende
 * de datos que quedaron de otra corrida. La unica prueba que **si** agota
 * un recurso compartido (el cubo de peticiones del gateway) vive en un
 * `describe` etiquetado `@limite`, omitido por defecto.
 */

const { test, expect } = require('@playwright/test');
const { API, tokenDe, elegirButacas, vigilar } = require('../helpers');

/**
 * El gateway tal y como lo ve el navegador: el front habla con
 * `http://localhost:8080` (`frontend/web/config.js`), no con el
 * `127.0.0.1` que usan las utilidades de la suite.
 */
const GATEWAY = 'http://localhost:8080';

/**
 * Vigila la pagina y, ademas, se queda con el origen de cada error de
 * consola.
 *
 * `vigilar` (el de `helpers.js`) junta cuatro senales en una lista de
 * textos, pero en los errores de consola **no guarda la URL**: sin ella no
 * se puede distinguir un error de la interfaz de un error que la propia
 * prueba provoque a proposito (un 401 sembrado se ve igual que un fallo de
 * la pagina). Por eso este envoltorio espia la consola por su cuenta: la
 * URL del error permite separar lo-provoking de lo real.
 *
 * @returns {{problemas: string[], consolaDe: (texto: string) => string[]}}
 */
function vigilarConOrigen(page) {
  const problemas = vigilar(page);
  const origenes = [];

  page.on('console', (mensaje) => {
    if (mensaje.type() !== 'error') return;
    const ubicacion = mensaje.location() || {};
    origenes.push({ texto: mensaje.text(), url: ubicacion.url || '' });
  });

  return {
    problemas,
    /** Las URLs de los errores de consola con ese texto exacto. */
    consolaDe: (texto) => origenes.filter((o) => o.texto === texto).map((o) => o.url),
  };
}

/**
 * Saca de la lista de problemas los que la prueba provoca a proposito: las
 * peticiones abortadas y las respuestas 401/503 sembradas. Esos no son
 * fallos de la interfaz.
 *
 * Lo que **si** es un fallo real se queda: una excepcion de JavaScript, un
 * error de consola que no viene de una URL que la prueba provoco, una
 * peticion fallida que no provoco la prueba o una respuesta 5xx que nadie
 * sembro.
 *
 * @param {string[]} problemas la lista de `vigilar`
 * @param {string[]} urlsProvocadas las URLs que la prueba intercepto
 * @param {(texto: string) => string[]} consolaDe origenes de los errores de consola
 */
function fallosReales(problemas, urlsProvocadas, consolaDe) {
  return problemas.filter((problema) => {
    // Una excepcion de JavaScript siempre es un problema real.
    if (problema.startsWith('excepcion:')) return true;
    if (problema.startsWith('peticion:') || problema.startsWith('respuesta:')) {
      return !urlsProvocadas.some((url) => problema.includes(url));
    }
    if (problema.startsWith('consola:')) {
      const texto = problema.slice('consola:'.length).trim();
      const origenes = consolaDe(texto);
      // El error de consola solo se perdona si **todos** los errores con ese
      // texto vienen de una URL que la prueba provoco.
      return !(
        origenes.length > 0 &&
        origenes.every((url) => urlsProvocadas.some((provocada) => url.includes(provocada)))
      );
    }
    return true;
  });
}

/**
 * Lee el catalogo real por la API, esperando a que el gateway conteste.
 *
 * El cubo del cliente es compartido con el resto de la suite: si esta
 * vacio, el gateway contesta 429 y no hay catalogo que leer. Se reintenta
 * con las aserciones de Playwright (sin esperas fijas).
 *
 * @param {import('@playwright/test').APIRequestContext} request
 * @returns {Promise<object>} el cuerpo del catalogo
 */
async function catalogoReal(request) {
  const token = await tokenDe(request, 'cliente');
  let cuerpo = null;
  await expect
    .poll(
      async () => {
        const respuesta = await request.get(`${API}/bff/web/catalogo`, {
          headers: { Authorization: `Bearer ${token}` },
        });
        if (respuesta.status() !== 200) return respuesta.status();
        cuerpo = await respuesta.json();
        return 200;
      },
      {
        timeout: 60_000,
        intervals: [500, 1000, 2000],
        message: 'el gateway no devolvio el catalogo',
      }
    )
    .toBe(200);
  return cuerpo;
}

/**
 * Intercepta una ruta del gateway dejando pasar el preflight.
 *
 * El navegador manda un `OPTIONS` antes de cada llamada con cabeceras de
 * autorizacion. En la version de Playwright que usa la suite esas
 * peticiones de preflight **no llegan al route** (se resuelven en la capa
 * de red), pero si llegaran, hay que dejarlas pasar: si el preflight se
 * intercepta, el navegador nunca deja salir la peticion real y la prueba
 * mediria otra cosa (un error de red en vez del 401 que se quiere sembrar).
 *
 * @param {import('@playwright/test').Page} page
 * @param {string} patron el patron de ruta de Playwright
 * @param {(route: import('@playwright/test').Route) => Promise<void>} manejar
 */
async function interceptar(page, patron, manejar) {
  await page.route(patron, async (route) => {
    if (route.request().method() === 'OPTIONS') {
      await route.continue();
      return;
    }
    await manejar(route);
  });
}

/**
 * Carga la pagina hasta que la interfaz muestre el aviso esperado.
 *
 * El gateway limita a 60 peticiones por minuto y 20 en rafaga por cliente, y
 * toda la suite comparte el mismo usuario de demostracion: si el cubo del
 * cliente esta vacio, el gateway contesta 429 y la interfaz cae al modo
 * demo (o el plano de la sala no carga). Eso no es lo que estas pruebas
 * miden, asi que la carga se reintenta con las aserciones de Playwright
 * (sin esperas fijas) hasta que el sistema conteste de verdad.
 *
 * @param {import('@playwright/test').Page} page
 * @param {string} esperado el texto del aviso ('' cuando no hay aviso)
 */
async function cargarHastaAviso(page, esperado) {
  await expect
    .poll(
      async () => {
        await page.goto('/');
        return (await page.locator('#aviso').textContent()).trim();
      },
      {
        timeout: 60_000,
        intervals: [500, 1000, 2000],
        message: `la interfaz no mostro el aviso esperado ("${esperado}")`,
      }
    )
    .toBe(esperado);
}

test.describe('Resiliencia y degradacion de la interfaz', () => {
  test('si el gateway no responde, la interfaz cae al modo demo y lo avisa', async ({ page }) => {
    const { problemas, consolaDe } = vigilarConOrigen(page);

    // El gateway no responde: se aborta cualquier peticion que la interfaz
    // le haga (el login y las llamadas al catalogo y al plano).
    const abortadas = [];
    await interceptar(page, `${GATEWAY}/**`, async (route) => {
      abortadas.push(`${route.request().method()} ${route.request().url()}`);
      await route.abort('failed');
    });

    // La pagina la sirve el BFF, asi que sigue cargando aunque el gateway
    // este muerto.
    await page.goto('/');

    // El aviso distingue "la API no respondio" de "las credenciales no
    // sirven": aqui es lo primero.
    await expect(page.locator('#aviso')).toHaveText(
      'Sin respuesta de la API: se muestran datos de demostración.',
      { timeout: 30_000 }
    );

    // Y se muestran datos de muestra: el programa y el plano de la sala
    // vienen de los datos de ejemplo del front.
    await expect(page.locator('#programa li button')).toHaveCount(3);
    await expect(page.locator('#programa')).toContainText('Cielo roto en vivo');
    await expect(page.locator('#plano button.butaca').first()).toBeVisible({ timeout: 30_000 });

    // Sin sesion abierta: en modo demo no hay token, asi que la barra no
    // muestra ninguna identidad.
    await expect(page.locator('#sesion')).toBeHidden();

    // La interfaz no se rompio: no hubo excepciones de JavaScript ni
    // errores que no sean las peticiones abortadas por la propia prueba.
    const reales = fallosReales(problemas, ['localhost:8080'], consolaDe);
    expect(reales, `la interfaz dejo errores: ${reales.join(' | ')}`).toEqual([]);
    expect(abortadas.length, 'la prueba no llego a interrumpir al gateway').toBeGreaterThan(0);
  });

  test('si el login falla, la interfaz avisa que no pudo iniciar sesion', async ({ page }) => {
    const { problemas, consolaDe } = vigilarConOrigen(page);

    // El gateway rechaza las credenciales.
    await interceptar(page, '**/api/v1/auth/token', async (route) => {
      await route.fulfill({
        status: 401,
        contentType: 'application/json',
        body: JSON.stringify({
          code: 'NO_AUTENTICADO',
          message: 'credenciales invalidas',
        }),
      });
    });

    await page.goto('/');

    // El aviso es el del login (no el de "sin respuesta"): el gateway
    // contesto, pero con un 401.
    await expect(page.locator('#aviso')).toHaveText(
      'No se pudo iniciar sesión en la API: se muestran datos de demostración.',
      { timeout: 30_000 }
    );

    // Sin sesion y con datos de muestra, igual que en el modo demo.
    await expect(page.locator('#sesion')).toBeHidden();
    await expect(page.locator('#programa')).toContainText('Cielo roto en vivo');

    const reales = fallosReales(problemas, ['localhost:8080/api/v1/auth/token'], consolaDe);
    expect(reales, `la interfaz dejo errores: ${reales.join(' | ')}`).toEqual([]);
  });

  test('un 401 en una llamada hace que la interfaz vuelva a iniciar sesion y reintente', async ({
    page,
  }) => {
    const { problemas, consolaDe } = vigilarConOrigen(page);

    // Se cuenta cada login: el handler del route lleva la cuenta.
    let logins = 0;
    await interceptar(page, '**/api/v1/auth/token', async (route) => {
      logins += 1;
      await route.continue();
    });

    // Solo la **primera** llamada al catalogo recibe el 401; las que
    // vengan despues pasan de verdad.
    let primera = true;
    await interceptar(page, '**/bff/web/catalogo', async (route) => {
      if (primera) {
        primera = false;
        await route.fulfill({
          status: 401,
          contentType: 'application/json',
          body: JSON.stringify({
            code: 'NO_AUTENTICADO',
            message: 'el token vencio',
          }),
        });
        return;
      }
      await route.continue();
    });

    await cargarHastaAviso(page, '');

    // El catalogo real termino cargandose: hay botones en el programa.
    await expect(page.locator('#programa li button').first()).toBeVisible({ timeout: 30_000 });

    // Y es el catalogo de verdad, no el de muestra: hay sesion abierta y no
    // aparecen los eventos de ejemplo.
    await expect(page.locator('#sesion')).toHaveText(/@/, { timeout: 30_000 });
    await expect(page.locator('#programa')).not.toContainText('Cielo roto en vivo');

    // Hubo re-login: el token se pidio mas de una vez.
    expect(logins, 'la interfaz no volvio a iniciar sesion tras el 401').toBeGreaterThan(1);

    // Y no queda aviso de error: el 401 se resolvio solo.
    await expect(page.locator('#aviso')).toBeHidden();

    const reales = fallosReales(problemas, ['localhost:8080/bff/web/catalogo'], consolaDe);
    expect(reales, `la interfaz dejo errores: ${reales.join(' | ')}`).toEqual([]);
  });

  test('si el catalogo llega degradado, la interfaz avisa y la compra no se completa', async ({
    page,
    request,
  }) => {
    const { problemas, consolaDe } = vigilarConOrigen(page);

    // El catalogo de verdad, para servir uno igual pero degradado.
    const catalogo = await catalogoReal(request);

    // Lo que responde el BFF cuando el servicio de pagos no esta: el
    // catalogo sigue, pero sin metodos de pago y marcado como degradado.
    const degradado = Object.assign({}, catalogo, {
      degradado: true,
      pagos_disponible: false,
    });
    delete degradado.metodos_pago;

    // Se elige un evento con butacas libres (la interfaz abre la primera
    // seccion del evento): hace falta para poder llegar al boton de
    // compra.
    const eventoConButacas = catalogo.eventos.find((evento) => {
      const suyas = catalogo.secciones.filter((s) => s.evento_id === evento.evento_id);
      return suyas.length > 0 && suyas[0].cantidad_disponible > 0;
    });
    expect(eventoConButacas, 'el catalogo no tiene ningun evento con butacas libres').toBeTruthy();

    // --- Escenario 1: el backend rechaza la compra -------------------------
    //
    // La compra se intercepta para que no llegue a ejecutarse una Saga de
    // verdad: el resto del sistema esta sano y la compra pasaria.
    await interceptar(page, '**/bff/web/catalogo', async (route) => {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify(degradado),
      });
    });
    const rechazo = {
      code: 'DEPENDENCY_UNAVAILABLE',
      message: 'el servicio de pagos no esta disponible',
    };
    await interceptar(page, '**/bff/web/compras', async (route) => {
      await route.fulfill({
        status: 503,
        contentType: 'application/json',
        body: JSON.stringify(rechazo),
      });
    });
    await interceptar(page, '**/api/v1/compras**', async (route) => {
      await route.fulfill({
        status: 503,
        contentType: 'application/json',
        body: JSON.stringify(rechazo),
      });
    });

    await cargarHastaAviso(
      page,
      'El servicio de pagos está degradado: se puede ver el catálogo, pero no comprar.'
    );

    // El catalogo se ve igual de bien: todos los eventos del programa.
    await expect(page.locator('#programa li button')).toHaveCount(catalogo.eventos.length);

    await page.locator('#programa li button', { hasText: eventoConButacas.nombre }).first().click();
    await expect(page.locator('#plano button.butaca:not([disabled])').first()).toBeVisible({
      timeout: 30_000,
    });

    // El boton de compra sigue ahi (el front **no** deshabilita la compra
    // cuando el catalogo llega degradado: avisa y deja que sea el backend
    // el que rechace la operacion), pero la compra no se completa.
    await elegirButacas(page, 1);
    await page.locator('#carrito button', { hasText: 'Comprar' }).click();

    // La compra falla y la interfaz lo dice: no hay entradas y el aviso
    // cuenta lo que paso.
    await expect(page.locator('#aviso')).toContainText('No se pudo completar la compra');
    await expect(page.locator('#entradas')).not.toContainText('CONFIRMADA');

    const reales = fallosReales(
      problemas,
      ['localhost:8080/bff/web/compras', 'localhost:8080/api/v1/compras'],
      consolaDe
    );
    expect(reales, `la interfaz dejo errores: ${reales.join(' | ')}`).toEqual([]);

    // --- Escenario 2: el backend compensa la saga -------------------------
    //
    // Esta es la respuesta real del BFF cuando el servicio de pagos esta
    // caido: la saga se compensa (no hay cobro ni entradas) y el BFF
    // responde 200 con la compra en estado COMPENSADA. La compra tampoco
    // se completa, aunque la interfaz anuncie la compra: hueco del front
    // que se reporta aparte.
    const otra = await page.context().newPage();
    const { problemas: problemas2, consolaDe: consola2 } = vigilarConOrigen(otra);
    await interceptar(otra, '**/bff/web/catalogo', async (route) => {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify(degradado),
      });
    });
    const compensada = {
      compra: {
        saga_id: 'saga-degradada',
        estado: 'COMPENSADA',
        monto_total_centavos: 0,
        moneda: 'CLP',
        pasos: [
          { nombre: 'RESERVAR', resultado: 'OK' },
          { nombre: 'COBRAR', resultado: 'ERROR' },
          { nombre: 'EMITIR', resultado: 'SKIPPED' },
        ],
      },
      entradas: [],
    };
    await interceptar(otra, '**/bff/web/compras', async (route) => {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify(compensada),
      });
    });
    await interceptar(otra, '**/api/v1/compras**', async (route) => {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify(compensada),
      });
    });

    await cargarHastaAviso(
      otra,
      'El servicio de pagos está degradado: se puede ver el catálogo, pero no comprar.'
    );

    await otra.locator('#programa li button', { hasText: eventoConButacas.nombre }).first().click();
    await expect(otra.locator('#plano button.butaca:not([disabled])').first()).toBeVisible({
      timeout: 30_000,
    });
    await elegirButacas(otra, 1);
    await otra.locator('#carrito button', { hasText: 'Comprar' }).click();

    // No hay compra confirmada: la saga quedo compensada y no hay entradas.
    await expect(otra.locator('#entradas')).toContainText('COMPENSADA');
    await expect(otra.locator('#entradas')).not.toContainText('CONFIRMADA');

    // Y la interfaz NO anuncia una compra que no ocurrio: el aviso dice que
    // se revirtio el pago, no "Entradas compradas.".
    await expect(otra.locator('#aviso')).toContainText(
      'La compra no se pudo completar: se revirtió el pago y no se emitieron entradas.'
    );
    await expect(otra.locator('#aviso')).not.toContainText('Entradas compradas.');

    const reales2 = fallosReales(
      problemas2,
      ['localhost:8080/bff/web/compras', 'localhost:8080/api/v1/compras'],
      consola2
    );
    expect(reales2, `la interfaz dejo errores: ${reales2.join(' | ')}`).toEqual([]);
    await otra.close();
  });
});

test.describe('@limite el limite de peticiones del gateway', () => {
  test.skip(
    !process.env.E2E_LIMITE,
    'esta prueba drena el cubo de peticiones del cliente y rompe el resto de la corrida: se activa con E2E_LIMITE=1'
  );

  test('una peticion que excede el limite del gateway responde 429 con Retry-After', async ({
    request,
  }) => {
    // Token de admin: el cubo de limite es por cliente y el admin tiene el
    // suyo propio (ademas, el login no consume peticiones del cubo).
    const token = await tokenDe(request, 'admin');
    const cabecera = { Authorization: `Bearer ${token}` };

    // Se drena el cubo del cliente hasta que el gateway conteste 429.
    let rechazo = null;
    for (let intento = 0; intento < 120 && !rechazo; intento += 1) {
      const respuesta = await request.get(`${API}/api/v1/butacas/eventos`, { headers: cabecera });
      if (respuesta.status() === 429) {
        rechazo = respuesta;
        break;
      }
      expect(respuesta.status(), 'la peticion deberia pasar hasta agotar el cubo').toBe(200);
    }

    expect(rechazo, 'el gateway nunca devolvio 429').not.toBeNull();
    expect(rechazo.status()).toBe(429);

    // La cabecera que dice cuanto hay que esperar.
    const retryAfter = rechazo.headers()['retry-after'];
    expect(retryAfter, 'la respuesta 429 no trae la cabecera Retry-After').toBeTruthy();
    expect(Number(retryAfter), 'el Retry-After no es un numero de segundos').toBeGreaterThanOrEqual(1);

    // Y el cuerpo con el codigo del borde.
    const cuerpo = await rechazo.json();
    expect(cuerpo.code).toBe('LIMITE_DE_PETICIONES');
  });
});
