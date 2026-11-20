/**
 * Pantalla de Administración: el CRUD de eventos y secciones.
 *
 * Es el trabajo de quien administra el recinto: entra a la pestaña
 * "Administrar", crea un evento, le agrega secciones, lo edita y lo
 * elimina. Cada prueba mira lo que la persona ve en pantalla y, cuando
 * aporta, lo que quedó guardado en el sistema (por la API del gateway).
 *
 * Cada prueba crea lo que necesita y lo borra al final, aunque falle: el
 * recinto no queda con basura de la suite (S8.3). Los eventos llevan una
 * marca temporal en el nombre para no chocar con otro dato del catálogo.
 *
 * Sobre el presupuesto de peticiones: el gateway limita a 60 peticiones por
 * minuto y por cliente, y la suite comparte el usuario de demostración
 * (S8.4). Por eso las **lecturas** de esta spec (listar eventos, listar
 * secciones, leer el plano) van con el token de *cliente* —esas rutas no
 * piden rol admin— y el token de *admin* se reserva para lo que sí lo
 * necesita: las escrituras y los borrados.
 */

const { test, expect } = require('@playwright/test');
const { API, tokenDe, borrarEvento, esperarCatalogo, vigilar } = require('../helpers');

/**
 * Marca temporal: dos pruebas que corren en el mismo segundo no pueden
 * crear eventos con el mismo nombre.
 */
function marca() {
  return `${Date.now()}-${Math.floor(Math.random() * 100000)}`;
}

/** Nombre del evento de la prueba (único dentro del catálogo). */
function nombreEvento(laMarca) {
  return `E2E admin ${laMarca}`;
}

/** Abre la pestaña "Administrar" y espera a que la vista esté en pantalla. */
async function abrirAdministracion(page) {
  await page.locator('button[data-vista="admin"]').click();
  await expect(page.locator('#vista-admin')).toBeVisible();
}

/**
 * La fila de un evento en la tabla de administración.
 *
 * El nombre se busca exacto en la primera celda (no por substring): si un
 * nombre fuera parte de otro, la fila buscada no sería la misma.
 */
function filaEvento(page, nombre) {
  return page.locator('#admin-eventos tr').filter({
    has: page.locator(`td:first-child:text-is("${nombre}")`),
  });
}

/**
 * Abre el formulario de evento, lo rellena y lo guarda.
 *
 * @returns {Promise<void>} cuando el formulario ya se envió
 */
async function guardarEventoPorInterfaz(page, datos) {
  await page.locator('#nuevo-evento').click();
  await expect(page.locator('#formulario-evento')).toBeVisible();
  await page.locator('#formulario-evento [name="nombre"]').fill(datos.nombre);
  await page.locator('#formulario-evento [name="lugar"]').fill(datos.lugar);
  await page.locator('#formulario-evento [name="fecha"]').fill(datos.fecha);
  await page.locator('#formulario-evento button[type="submit"]').click();
}

/**
 * Una lectura por la API que respeta el límite de peticiones del gateway.
 *
 * El gateway limita a 60 peticiones por minuto y por cliente y contesta 429
 * con `Retry-After` cuando se pasa: la suite comparte el usuario de
 * demostración (S8.4), así que esta lectura espera lo que el gateway pide y
 * reintenta, en vez de fallar por un límite conocido y ajeno a la prueba.
 *
 * @returns {Promise<object>} el cuerpo JSON de la respuesta
 */
async function leerPorApi(request, ruta, token, intentos = 5) {
  for (let intento = 1; intento <= intentos; intento += 1) {
    const respuesta = await request.get(ruta, {
      headers: { Authorization: `Bearer ${token}` },
    });
    if (respuesta.status() !== 429) {
      expect(respuesta.status(), `no se pudo leer ${ruta}`).toBe(200);
      return respuesta.json();
    }
    const espera = Number(respuesta.headers()['retry-after']) || 1;
    await new Promise((resolver) => setTimeout(resolver, espera * 1000));
  }
  throw new Error(`${ruta} siguió respondiendo 429 después de ${intentos} intentos`);
}

/**
 * Los eventos del recinto por la API: lo que quedó persistido de verdad.
 *
 * @returns {Promise<object[]>} la lista de eventos
 */
async function eventosPorApi(request, token) {
  const cuerpo = await leerPorApi(request, `${API}/api/v1/butacas/eventos`, token);
  return cuerpo.eventos || [];
}

/**
 * Red de seguridad del `finally`: borra el evento que creó la prueba, si
 * todavía existe (borrar el evento se lleva sus secciones).
 *
 * Si la prueba ya sabe el identificador, borra directo; si no —porque la
 * prueba falló antes de averiguarlo— lo busca por nombre. Nunca lanza: un
 * error de limpieza no debe tapar el error real de la prueba.
 *
 * @param {object} evento lo que la prueba sabe del evento creado
 * @returns {Promise<string|null>} el identificador del evento borrado
 */
async function limpiarEvento(request, evento, token) {
  if (evento.borrado) return null;
  if (!evento.id) {
    let eventos = [];
    try {
      eventos = await eventosPorApi(request, token);
    } catch (error) {
      return null;
    }
    const encontrado = eventos.find((item) => item.nombre === evento.nombre);
    if (!encontrado) return null;
    evento.id = encontrado.evento_id;
  }
  await borrarEvento(request, evento.id, token);
  return evento.id;
}

test.describe('Administración de eventos y secciones', () => {
  test('el formulario de evento arranca oculto y "Nuevo evento" lo abre vacío', async ({ page }) => {
    const problemas = vigilar(page);

    await page.goto('/');
    await esperarCatalogo(page);
    await abrirAdministracion(page);

    // El formulario no se ve hasta que alguien lo pide.
    await expect(page.locator('#formulario-evento')).toBeHidden();

    await page.locator('#nuevo-evento').click();

    // Ahora sí: abierto, en modo "crear" y sin datos de nadie.
    await expect(page.locator('#formulario-evento')).toBeVisible();
    await expect(page.locator('#formulario-titulo')).toHaveText('Nuevo evento');
    await expect(page.locator('#formulario-evento [name="nombre"]')).toHaveValue('');
    await expect(page.locator('#formulario-evento [name="lugar"]')).toHaveValue('');
    await expect(page.locator('#formulario-evento [name="fecha"]')).toHaveValue('');

    expect(problemas, `la interfaz dejó errores: ${problemas.join(' | ')}`).toEqual([]);
  });

  test('crear un evento lo muestra en la tabla y avisa que quedó guardado', async ({
    page,
    request,
  }) => {
    const problemas = vigilar(page);
    const tokenAdmin = await tokenDe(request, 'admin');
    const tokenCliente = await tokenDe(request, 'cliente');
    const nombre = nombreEvento(marca());
    const lugar = 'Recinto E2E';
    const fecha = '2027-02-20T18:00';
    const evento = { id: null, nombre, borrado: false };

    try {
      await page.goto('/');
      await esperarCatalogo(page);
      await abrirAdministracion(page);

      await guardarEventoPorInterfaz(page, { nombre, lugar, fecha });

      // La persona ve su evento en la tabla de administración.
      await expect(filaEvento(page, nombre)).toHaveCount(1);
      await expect(filaEvento(page, nombre)).toContainText(lugar);
      // Y recibe el aviso del guardado.
      await expect(page.locator('#aviso')).toHaveText('Evento guardado.');
      // El formulario se cierra solo: ya no queda abierto en la pantalla.
      await expect(page.locator('#formulario-evento')).toBeHidden();

      // El sistema guardó lo mismo que la pantalla-annuncia.
      const eventos = await eventosPorApi(request, tokenCliente);
      const guardado = eventos.find((item) => item.nombre === nombre);
      expect(guardado, 'el evento no quedó guardado en el sistema').toBeTruthy();
      expect(guardado.lugar, 'el recinto guardado no es el que se escribió').toBe(lugar);
      expect(
        guardado.fecha.startsWith('2027-02-20T18:00'),
        `la fecha guardada no es la escrita: ${guardado.fecha}`,
      ).toBe(true);
      evento.id = guardado.evento_id;

      expect(problemas, `la interfaz dejó errores: ${problemas.join(' | ')}`).toEqual([]);
    } finally {
      await limpiarEvento(request, evento, tokenAdmin);
    }

    // La suite no deja basura: el evento de la prueba ya no existe.
    const restantes = await eventosPorApi(request, tokenCliente);
    expect(restantes.filter((item) => item.nombre === nombre)).toHaveLength(0);
  });

  test('editar un evento cambia lo que muestra la tabla', async ({ page, request }) => {
    const problemas = vigilar(page);
    const tokenAdmin = await tokenDe(request, 'admin');
    const tokenCliente = await tokenDe(request, 'cliente');
    const nombre = nombreEvento(marca());
    // El nombre nuevo lleva su propia marca: si compartiera prefijo con el
    // viejo, la fila del evento renombrado seguiría "contendiendo" con la
    // del evento que se creó arriba.
    const nombreNuevo = nombreEvento(marca());
    const lugar = 'Recinto E2E';
    const fecha = '2027-02-20T18:00';
    const evento = { id: null, nombre: nombreNuevo, borrado: false };

    try {
      await page.goto('/');
      await esperarCatalogo(page);
      await abrirAdministracion(page);

      await guardarEventoPorInterfaz(page, { nombre, lugar, fecha });
      await expect(filaEvento(page, nombre)).toHaveCount(1);

      // "Editar" abre el formulario con los datos del evento.
      await filaEvento(page, nombre).getByRole('button', { name: 'Editar', exact: true }).click();
      await expect(page.locator('#formulario-evento')).toBeVisible();
      await expect(page.locator('#formulario-titulo')).toHaveText('Editar evento');
      await expect(page.locator('#formulario-evento [name="nombre"]')).toHaveValue(nombre);
      await expect(page.locator('#formulario-evento [name="lugar"]')).toHaveValue(lugar);
      await expect(page.locator('#formulario-evento [name="fecha"]')).toHaveValue(fecha);

      // Se cambia el nombre y se guarda.
      await page.locator('#formulario-evento [name="nombre"]').fill(nombreNuevo);
      await page.locator('#formulario-evento button[type="submit"]').click();

      // La tabla muestra el nombre nuevo y el viejo ya no está.
      await expect(filaEvento(page, nombreNuevo)).toHaveCount(1);
      await expect(filaEvento(page, nombre)).toHaveCount(0);
      await expect(page.locator('#aviso')).toHaveText('Evento guardado.');

      // Y el sistema guardó el nombre nuevo, no el anterior.
      const eventos = await eventosPorApi(request, tokenCliente);
      const guardado = eventos.find((item) => item.nombre === nombreNuevo);
      expect(guardado, 'el evento no quedó guardado con el nombre nuevo').toBeTruthy();
      expect(
        eventos.some((item) => item.nombre === nombre),
        'el evento con el nombre viejo sigue en el sistema',
      ).toBe(false);
      evento.id = guardado.evento_id;

      expect(problemas, `la interfaz dejó errores: ${problemas.join(' | ')}`).toEqual([]);
    } finally {
      // Si el renombrado falló, el evento sigue con el nombre viejo: la
      // limpieza lo busca por los dos nombres para no dejarlo en el recinto.
      await limpiarEvento(request, evento, tokenAdmin);
      await limpiarEvento(request, { id: null, nombre, borrado: false }, tokenAdmin);
    }

    const restantes = await eventosPorApi(request, tokenCliente);
    expect(
      restantes.filter((item) => item.nombre === nombre || item.nombre === nombreNuevo),
    ).toHaveLength(0);
  });

  test('agregar una sección la muestra con su capacidad y su precio', async ({
    page,
    request,
  }) => {
    const problemas = vigilar(page);
    const tokenAdmin = await tokenDe(request, 'admin');
    const tokenCliente = await tokenDe(request, 'cliente');
    const laMarca = marca();
    const nombre = nombreEvento(laMarca);
    const nombreSeccion = `Platea ${laMarca}`;
    const lugar = 'Recinto E2E';
    const fecha = '2027-02-20T18:00';
    const capacidad = 12;
    const precio = 25000; // pesos; la interfaz los guarda en centavos
    const evento = { id: null, nombre, borrado: false };

    try {
      await page.goto('/');
      await esperarCatalogo(page);
      await abrirAdministracion(page);

      await guardarEventoPorInterfaz(page, { nombre, lugar, fecha });
      await expect(filaEvento(page, nombre)).toHaveCount(1);

      // Las secciones son de un evento: primero se elige en la tabla.
      await filaEvento(page, nombre).getByRole('button', { name: 'Secciones', exact: true }).click();
      await expect(page.locator('#secciones-evento')).toHaveText(nombre);

      await page.locator('#nueva-seccion').click();
      await expect(page.locator('#formulario-seccion')).toBeVisible();
      await page.locator('#formulario-seccion [name="nombre"]').fill(nombreSeccion);
      await page.locator('#formulario-seccion [name="capacidad_total"]').fill(String(capacidad));
      await page.locator('#formulario-seccion [name="precio"]').fill(String(precio));
      await page.locator('#formulario-seccion button[type="submit"]').click();

      // La sección aparece en la tabla con su capacidad y su precio.
      const fila = page.locator('#admin-secciones-tbody tr', { hasText: nombreSeccion });
      await expect(fila, 'la sección no apareció en la tabla').toHaveCount(1);
      await expect(fila).toContainText(String(capacidad));
      await expect(fila).toContainText('$25.000');
      await expect(page.locator('#aviso')).toHaveText('Sección guardada.');

      // Y el sistema guardó la sección con esos números.
      const eventos = await eventosPorApi(request, tokenCliente);
      const guardado = eventos.find((item) => item.nombre === nombre);
      expect(guardado, 'el evento de la sección no está en el sistema').toBeTruthy();
      evento.id = guardado.evento_id;
      const cuerpoSecciones = await leerPorApi(
        request,
        `${API}/api/v1/butacas/secciones?evento_id=${encodeURIComponent(guardado.evento_id)}`,
        tokenCliente,
      );
      const secciones = cuerpoSecciones.secciones || [];
      const seccion = secciones.find((item) => item.nombre === nombreSeccion);
      expect(seccion, 'la sección no quedó guardada en el sistema').toBeTruthy();
      expect(seccion.capacidad_total, 'la capacidad guardada no es la escrita').toBe(capacidad);
      expect(seccion.precio_centavos, 'el precio guardado no es el escrito').toBe(precio * 100);

      // Y sus butacas quedaron escritas: la sección se guarda antes que su
      // plano, así que la base no rechaza ninguna butaca huérfana.
      const cuerpoPlano = await leerPorApi(
        request,
        `${API}/api/v1/butacas/secciones/${encodeURIComponent(seccion.seccion_id)}/butacas`,
        tokenCliente,
      );
      const butacas = cuerpoPlano.butacas || [];
      expect(butacas.length, 'la sección no tiene sus butacas').toBe(capacidad);
      expect(
        butacas.every((butaca) => butaca.estado === 'LIBRE'),
        'las butacas de la sección nueva no están libres',
      ).toBe(true);

      expect(problemas, `la interfaz dejó errores: ${problemas.join(' | ')}`).toEqual([]);
    } finally {
      // Borrar el evento se lleva sus secciones: una sola limpieza.
      await limpiarEvento(request, evento, tokenAdmin);
    }

    const restantes = await eventosPorApi(request, tokenCliente);
    expect(restantes.filter((item) => item.nombre === nombre)).toHaveLength(0);
  });

  test('cancelar cierra el formulario de evento y lo deja vacío', async ({ page }) => {
    const problemas = vigilar(page);

    await page.goto('/');
    await esperarCatalogo(page);
    await abrirAdministracion(page);

    await page.locator('#nuevo-evento').click();
    await expect(page.locator('#formulario-evento')).toBeVisible();

    // Se escriben datos para que "vacío" signifique algo.
    await page.locator('#formulario-evento [name="nombre"]').fill('Evento que se descarta');
    await page.locator('#formulario-evento [name="lugar"]').fill('Recinto descartado');
    await page.locator('#formulario-evento [name="fecha"]').fill('2027-02-20T18:00');

    await page.locator('#cancelar-evento').click();

    // El formulario se cierra y no guarda nada de lo que se había escrito.
    await expect(page.locator('#formulario-evento')).toBeHidden();
    await expect(page.locator('#formulario-evento [name="nombre"]')).toHaveValue('');
    await expect(page.locator('#formulario-evento [name="lugar"]')).toHaveValue('');
    await expect(page.locator('#formulario-evento [name="fecha"]')).toHaveValue('');
    await expect(filaEvento(page, 'Evento que se descarta')).toHaveCount(0);

    expect(problemas, `la interfaz dejó errores: ${problemas.join(' | ')}`).toEqual([]);
  });

  test('eliminar un evento lo saca de la tabla', async ({ page, request }) => {
    const problemas = vigilar(page);
    const tokenAdmin = await tokenDe(request, 'admin');
    const tokenCliente = await tokenDe(request, 'cliente');
    const nombre = nombreEvento(marca());
    const lugar = 'Recinto E2E';
    const fecha = '2027-02-20T18:00';
    const evento = { id: null, nombre, borrado: false };

    // El borrado pide confirmación: sin aceptar el diálogo, no se borra nada.
    page.on('dialog', (dialogo) => dialogo.accept());

    try {
      await page.goto('/');
      await esperarCatalogo(page);
      await abrirAdministracion(page);

      await guardarEventoPorInterfaz(page, { nombre, lugar, fecha });
      await expect(filaEvento(page, nombre)).toHaveCount(1);

      await filaEvento(page, nombre).getByRole('button', { name: 'Eliminar', exact: true }).click();

      // La fila desaparece de la tabla...
      await expect(filaEvento(page, nombre)).toHaveCount(0);
      // ...y el sistema ya no lo tiene.
      const eventos = await eventosPorApi(request, tokenCliente);
      expect(
        eventos.some((item) => item.nombre === nombre),
        'el evento sigue en el sistema después de eliminarlo',
      ).toBe(false);
      evento.borrado = true;

      expect(problemas, `la interfaz dejó errores: ${problemas.join(' | ')}`).toEqual([]);
    } finally {
      await limpiarEvento(request, evento, tokenAdmin);
    }

    const restantes = await eventosPorApi(request, tokenCliente);
    expect(restantes.filter((item) => item.nombre === nombre)).toHaveLength(0);
  });
});
