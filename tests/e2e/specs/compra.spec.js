/**
 * Flujo central de la ticketera, de punta a punta y por la interfaz.
 *
 * Es el camino que hace la persona: entra, elige evento, elige butacas,
 * paga y recibe sus entradas. La prueba comprueba lo que ve en pantalla y,
 * despues, que el sistema guardo lo mismo que la pantalla-annuncio.
 *
 * El evento de la prueba se crea por la API (para tener butacas libres
 * garantizadas) y se borra al final: la prueba no deja basura.
 */

const { test, expect } = require('@playwright/test');
const {
  API,
  tokenDe,
  crearEventoConSeccion,
  borrarEvento,
  esperarCatalogo,
  elegirEvento,
  elegirButacas,
  comprar,
  vigilar,
} = require('../helpers');

test.describe('Compra de entradas', () => {
  test('el comprador elige evento, butacas y compra; la saga queda confirmada', async ({
    page,
    request,
  }) => {
    const problemas = vigilar(page);
    const { evento, seccion } = await crearEventoConSeccion(request, {
      capacidad: 8,
      precio: 12000,
    });

    try {
      await page.goto('/');
      await esperarCatalogo(page);

      // El evento recien creado aparece en el programa de la interfaz.
      await elegirEvento(page, evento.nombre);

      // El plano trae las butacas de la seccion, todas libres.
      const butacas = page.locator('#plano button.butaca');
      await expect(butacas).toHaveCount(seccion.capacidad_total);
      await expect(page.locator('#plano button.butaca[disabled]')).toHaveCount(0);

      const elegidas = await elegirButacas(page, 2);
      expect(elegidas).toHaveLength(2);

      // El carrito muestra el total: dos entradas por el precio de la seccion.
      await expect(page.locator('#carrito')).toContainText('2 entradas');
      await expect(page.locator('#carrito')).toContainText('$240');

      const saga_id = await comprar(page);

      // La pantalla entrega la compra con su estado y su codigo de entrada.
      await expect(page.locator('#entradas')).toContainText(evento.nombre);
      await expect(page.locator('#entradas')).toContainText('CONFIRMADA');
      await expect(page.locator('#entradas [role="img"]')).toBeVisible();
      await expect(page.locator('#entradas')).toContainText(/\$[0-9]/);

      // Y el sistema guardo lo mismo que la pantalla-annuncio: la saga esta
      // confirmada, con sus tres pasos en orden y el total esperado.
      const token = await tokenDe(request, 'cliente');
      const consulta = await request.get(`${API}/bff/web/compras/${saga_id}`, {
        headers: { Authorization: `Bearer ${token}` },
      });
      expect(consulta.status()).toBe(200);
      const cuerpo = await consulta.json();
      const compra = cuerpo.compra;

      expect(compra.estado).toBe('CONFIRMADA');
      expect(compra.saga_id).toBe(saga_id);
      expect(compra.cliente_id).toBe('cliente-1');
      expect(compra.monto_total_centavos).toBe(24000);
      expect(compra.pasos.map((paso) => paso.nombre)).toEqual(['RESERVAR', 'COBRAR', 'EMITIR']);
      expect(compra.pasos.map((paso) => paso.resultado)).toEqual(['OK', 'OK', 'OK']);
      expect(cuerpo.entradas).toHaveLength(2);

      expect(problemas, `la interfaz dejo errores: ${problemas.join(' | ')}`).toEqual([]);
    } finally {
      await borrarEvento(request, evento.evento_id);
    }
  });

  test('la butaca comprada deja de estar libre para el siguiente comprador', async ({
    page,
    request,
  }) => {
    const { evento } = await crearEventoConSeccion(request, { capacidad: 4, precio: 9000 });

    try {
      await page.goto('/');
      await esperarCatalogo(page);
      await elegirEvento(page, evento.nombre);

      const libres = page.locator('#plano button.butaca:not([disabled])');
      expect(await libres.count()).toBe(4);

      await elegirButacas(page, 1);
      await comprar(page);

      // El plano de una sesion nueva muestra la butaca ocupada: la venta no
      // se puede repetir, ni desde el navegador ni desde la API.
      const otraPagina = await page.context().newPage();
      await otraPagina.goto('/');
      await esperarCatalogo(otraPagina);
      await elegirEvento(otraPagina, evento.nombre);

      await expect(otraPagina.locator('#plano button.butaca[disabled]')).toHaveCount(1);
      await expect(otraPagina.locator('#plano button.butaca:not([disabled])')).toHaveCount(3);
      await otraPagina.close();
    } finally {
      await borrarEvento(request, evento.evento_id);
    }
  });
});
