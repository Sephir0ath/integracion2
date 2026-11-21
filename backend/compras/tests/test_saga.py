"""Tests of the saga orchestrator.

They cover the three paths of the saga: the happy path, the compensation
of the steps that were confirmed (in the **reverse** order) and the
failure that leaves nothing to compensate.

The order of the compensations is the core requirement of the saga, so
the tests assert the order of the calls, not only that they happened.
"""

from __future__ import annotations

import asyncio
import time

import pytest

from common.errors import CODES, ServiceError

from .fakes import FakeButacas, FakeEmision, FakePagos, error_de_dependencia

PRECIO = 25_000_000


async def _compra(servicio, **cambios):
    """Start a purchase with the usual arguments."""
    datos = {
        "saga_id": "saga-1",
        "evento_id": "ev-1",
        "seccion_id": "sec-1",
        "butaca_ids": ["sec-1-A1", "sec-1-A2"],
        "cliente_id": "cli-1",
        "moneda": "COP",
    }
    datos.update(cambios)
    return await servicio.iniciar_compra(**datos)


# ------------------------------------------------------------ camino feliz --
async def test_camino_felig_confirma_los_tres_pasos(servicio, llamadas):
    resultado = await _compra(servicio)

    assert resultado["estado"] == "CONFIRMADA"
    assert [paso["nombre"] for paso in resultado["pasos"]] == ["RESERVAR", "COBRAR", "EMITIR"]
    assert all(paso["resultado"] == "OK" for paso in resultado["pasos"])
    assert resultado["compensaciones"] == []
    assert resultado["error"] == ""
    assert llamadas.nombres == [
        "butacas.reservar",
        "pagos.cobrar",
        "emision.emitir",
        "butacas.confirmar_venta",
    ]


async def test_el_precio_lo_define_el_dominio(servicio, pagos):
    """The amount charged comes from the reservation, not from the client."""
    await _compra(servicio)

    assert len(pagos.cobros) == 1
    assert pagos.cobros[0]["monto_centavos"] == PRECIO * 2
    assert pagos.cobros[0]["moneda"] == "COP"


async def test_la_compra_confirmada_reporta_el_monto_y_la_cantidad(servicio):
    resultado = await _compra(servicio)

    assert resultado["monto_total_centavos"] == PRECIO * 2
    assert resultado["cantidad"] == 2
    assert resultado["moneda"] == "COP"
    assert resultado["saga_id"] == "saga-1"


async def test_la_emision_recibe_el_pago_y_las_butacas_reservadas(servicio, emision):
    await _compra(servicio)

    assert len(emision.emitidos) == 1
    emitido = emision.emitidos[0]
    assert emitido["pago_id"] == "pg-saga-1"
    assert [butaca["butaca_id"] for butaca in emitido["butacas"]] == ["sec-1-A1", "sec-1-A2"]


async def test_las_claves_de_idempotencia_llevan_el_nombre_del_paso(servicio, llamadas):
    await _compra(servicio)

    claves = {nombre: clave for nombre, _, clave in llamadas.registro}
    assert claves["butacas.reservar"] == "saga-1:RESERVAR"
    assert claves["pagos.cobrar"] == "saga-1:COBRAR"
    assert claves["emision.emitir"] == "saga-1:EMITIR"


# ------------------------------------------------------------ compensacion --
async def test_si_falla_el_ultimo_paso_se_compensa_en_orden_inverso(sesiones, llamadas):
    """The failure of `EMITIR` compensates `COBRAR` and then `RESERVAR`."""
    emision = FakeEmision(llamadas, error=error_de_dependencia())
    pagos = FakePagos(llamadas)
    butacas = FakeButacas(llamadas)
    from ..service import ComprasService

    servicio = ComprasService(sesiones, butacas, pagos, emision, deadline=0.5)
    resultado = await _compra(servicio)

    assert resultado["estado"] == "COMPENSADA"
    assert [comp["nombre"] for comp in resultado["compensaciones"]] == ["REEMBOLSAR", "LIBERAR"]
    assert all(comp["resultado"] == "OK" for comp in resultado["compensaciones"])
    # El orden real de las llamadas es el inverso de los pasos confirmados.
    assert llamadas.nombres == [
        "butacas.reservar",
        "pagos.cobrar",
        "emision.emitir",
        "pagos.reembolsar",
        "butacas.liberar",
    ]


async def test_si_falla_el_cobro_solo_se_libera_la_reserva(sesiones, llamadas):
    """The failure of `COBRAR` compensates only `RESERVAR`."""
    pagos = FakePagos(llamadas, error=error_de_dependencia(CODES.DEPENDENCY_UNAVAILABLE))
    butacas = FakeButacas(llamadas)
    emision = FakeEmision(llamadas)
    from ..service import ComprasService

    servicio = ComprasService(sesiones, butacas, pagos, emision, deadline=0.5)
    resultado = await _compra(servicio)

    assert resultado["estado"] == "COMPENSADA"
    assert [comp["nombre"] for comp in resultado["compensaciones"]] == ["LIBERAR"]
    assert "emision.emitir" not in llamadas.nombres
    assert "pagos.reembolsar" not in llamadas.nombres


async def test_si_falla_la_reserva_no_hay_nada_que_compensar(sesiones, llamadas):
    """The failure of the first step leaves nothing confirmed."""
    butacas = FakeButacas(llamadas, error=ServiceError(CODES.ALREADY_EXISTS, "la butaca ya no esta libre"))
    pagos = FakePagos(llamadas)
    emision = FakeEmision(llamadas)
    from ..service import ComprasService

    servicio = ComprasService(sesiones, butacas, pagos, emision, deadline=0.5)
    resultado = await _compra(servicio)

    assert resultado["estado"] == "FALLIDA"
    assert resultado["compensaciones"] == []
    assert resultado["error"]
    # Los pasos que nunca corrieron quedan marcados como omitidos.
    omitidos = [p["nombre"] for p in resultado["pasos"] if p["resultado"] == "SKIPPED"]
    assert omitidos == ["COBRAR", "EMITIR"]
    assert "pagos.cobrar" not in llamadas.nombres


async def test_una_compensacion_fallida_deja_la_saga_fallida(sesiones, llamadas):
    """A compensation that fails is reported, and the saga is not compensated."""
    emision = FakeEmision(llamadas, error=error_de_dependencia())
    pagos = FakePagos(llamadas, error_reembolso=error_de_dependencia())
    butacas = FakeButacas(llamadas)
    from ..service import ComprasService

    servicio = ComprasService(sesiones, butacas, pagos, emision, deadline=0.5)
    resultado = await _compra(servicio)

    assert resultado["estado"] == "FALLIDA"
    resultados = {comp["nombre"]: comp["resultado"] for comp in resultado["compensaciones"]}
    assert resultados["REEMBOLSAR"] == "ERROR"
    # La compensacion que si podia correr, corrio: no se abandona a mitad.
    assert resultados["LIBERAR"] == "OK"


async def test_si_falla_el_cierre_de_venta_se_compensa_todo(sesiones, llamadas):
    """`ConfirmarVenta` is the closing of the sale: if it fails, the saga compensates."""
    butacas = FakeButacas(llamadas, error_confirmar=error_de_dependencia())
    pagos = FakePagos(llamadas)
    emision = FakeEmision(llamadas)
    from ..service import ComprasService

    servicio = ComprasService(sesiones, butacas, pagos, emision, deadline=0.5)
    resultado = await _compra(servicio)

    assert resultado["estado"] == "COMPENSADA"
    assert [comp["nombre"] for comp in resultado["compensaciones"]] == [
        "ANULAR",
        "REEMBOLSAR",
        "LIBERAR",
    ]


# ------------------------------------------------------------ idempotencia --
async def test_la_compra_repetida_no_repite_los_pasos(servicio, llamadas):
    primero = await _compra(servicio)
    llamadas.registro.clear()

    segundo = await _compra(servicio)

    assert primero["estado"] == "CONFIRMADA"
    assert segundo["estado"] == "CONFIRMADA"
    assert segundo["idempotente"] is True
    # Ninguna llamada nueva a las dependencias.
    assert llamadas.registro == []


async def test_la_compra_repetida_con_otra_peticion_es_conflicto(servicio):
    await _compra(servicio)

    with pytest.raises(ServiceError) as excepcion:
        await _compra(servicio, butaca_ids=["sec-1-B9"])

    assert excepcion.value.code == CODES.CONFLICT_IDEMPOTENCIA


async def test_el_estado_de_la_saga_queda_consultable(servicio):
    await _compra(servicio)

    consultada = await servicio.consultar_compra("saga-1")

    assert consultada["estado"] == "CONFIRMADA"
    assert [paso["nombre"] for paso in consultada["pasos"]] == ["RESERVAR", "COBRAR", "EMITIR"]
    assert consultada["monto_total_centavos"] == PRECIO * 2


async def test_consultar_una_saga_inexistente_falla(servicio):
    with pytest.raises(ServiceError) as excepcion:
        await servicio.consultar_compra("saga-que-no-existe")

    assert excepcion.value.code == CODES.NOT_FOUND


# ----------------------------------------------------------------- limites --
async def test_un_paso_que_se_cuelga_no_tiene_la_saga_esperando(sesiones, llamadas):
    """Every call between services has a time limit."""
    emision = FakeEmision(llamadas, delay=5.0)
    pagos = FakePagos(llamadas)
    butacas = FakeButacas(llamadas)
    from ..service import ComprasService

    servicio = ComprasService(sesiones, butacas, pagos, emision, deadline=0.2)
    resultado = await _compra(servicio)

    assert resultado["estado"] == "COMPENSADA"
    assert resultado["pasos"][2]["nombre"] == "EMITIR"
    assert resultado["pasos"][2]["resultado"] == "ERROR"


async def test_una_peticion_incompleta_es_invalida(servicio):
    with pytest.raises(ServiceError) as excepcion:
        await _compra(servicio, saga_id="")

    assert excepcion.value.code == CODES.INVALID


async def test_una_compra_sin_butacas_es_invalida(servicio):
    with pytest.raises(ServiceError) as excepcion:
        await _compra(servicio, butaca_ids=[])

    assert excepcion.value.code == CODES.INVALID


async def test_una_dependencia_lenta_no_deja_la_saga_colgada(sesiones, llamadas, butacas, emision):
    """A dependency that does not answer leaves the saga compensated, not hanging.

    The deadline of the orchestrator has to be honored: before, the step
    never reached the error path (the deadline waited for the cancellation
    of a call that took long to wind down), so the saga stayed `RUNNING`
    forever, with its seat reserved and without compensating anything.
    """
    from ..service import ComprasService

    # El pago tarda mas que el deadline del orquestador.
    # La llamada ignora la cancelacion, como una gRPC en vuelo.
    pagos = FakePagos(llamadas, delay=2.0, ignora_cancelacion=True)
    servicio = ComprasService(sesiones, butacas, pagos, emision, deadline=0.2)

    inicio = time.perf_counter()
    resultado = await _compra(servicio)
    elapsed = time.perf_counter() - inicio

    assert resultado["estado"] == "COMPENSADA"
    assert elapsed < 1.0, f"el deadline no se cumplio ({elapsed:.2f}s)"
    # La butaca se libero: no queda reservada por una saga colgada.
    assert llamadas.nombres == ["butacas.reservar", "pagos.cobrar", "butacas.liberar"]
    assert [comp["nombre"] for comp in resultado["compensaciones"]] == ["LIBERAR"]


async def test_la_saga_termina_although_el_cliente_cancele(sesiones, llamadas, butacas, emision):
    """The saga completes and compensates even if the client goes away.

    The client can disappear: the edge has its own deadline and gives up
    when it is met. The saga is not tied to the request, so it keeps
    running and compensates the seat it had reserved; otherwise the seat
    stayed reserved forever, with the saga stuck in `RUNNING`.
    """
    from ..service import ComprasService

    pagos = FakePagos(llamadas, delay=2.0, ignora_cancelacion=True)
    servicio = ComprasService(sesiones, butacas, pagos, emision, deadline=0.2)

    tarea = asyncio.create_task(_compra(servicio))
    await asyncio.sleep(0.05)
    tarea.cancel()  # el cliente se va
    with pytest.raises(asyncio.CancelledError):
        await tarea

    # La saga sigue en segundo plano: se le da tiempo a que termine.
    await asyncio.sleep(0.8)

    saga = await servicio.consultar_compra("saga-1")
    assert saga["estado"] == "COMPENSADA"
    assert "butacas.liberar" in llamadas.nombres
