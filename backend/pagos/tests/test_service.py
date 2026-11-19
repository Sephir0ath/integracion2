"""Tests of the domain logic of the `pagos` service.

They cover the idempotency of the saga step `COBRAR` and of its
compensation `REEMBOLSAR`, and the rollback of a charge that could not be
made. The database session and the gateway are fakes, so the tests need
neither PostgreSQL nor a running pasarela.
"""

from __future__ import annotations

import pytest

from common.errors import CODES, ServiceError
from common.idempotency import IdempotencyRecord

from ..models import CONFIRMADO, REEMBOLSADO, Pago
from ..service import PagosService, payment_id_for
from .fakes import FakePasarela, FakeSessionProvider


@pytest.fixture()
def sesiones():
    return FakeSessionProvider()


@pytest.fixture()
def pasarela():
    return FakePasarela()


@pytest.fixture()
def servicio(sesiones, pasarela):
    return PagosService(sesiones, pasarela)


# ------------------------------------------------------------------ cobro --
async def test_cobrar_registra_el_pago(servicio, pasarela):
    resultado = await servicio.cobrar("saga-1", 25000, "COP")

    assert resultado["idempotente"] is False
    assert resultado["pago"]["estado"] == CONFIRMADO
    assert resultado["pago"]["monto_centavos"] == 25000
    assert resultado["pago"]["moneda"] == "COP"
    assert resultado["pago"]["saga_id"] == "saga-1"
    assert resultado["pago"]["pasarela_ref"] == "pgw_falso"
    assert pasarela.calls == [("cobrar", 25000, "saga-1:COBRAR")]


async def test_cobrar_es_idempotente(servicio, pasarela):
    primera = await servicio.cobrar("saga-1", 25000, "COP")
    segunda = await servicio.cobrar("saga-1", 25000, "COP")

    assert segunda["idempotente"] is True
    assert segunda["pago"] == primera["pago"]
    assert segunda["pago"]["pago_id"] == primera["pago"]["pago_id"]
    assert len(pasarela.calls) == 1, "un comando repetido no puede cobrar dos veces"


async def test_la_misma_clave_con_otro_payload_es_conflicto(servicio, pasarela):
    await servicio.cobrar("saga-1", 25000, "COP")

    with pytest.raises(ServiceError) as error:
        await servicio.cobrar("saga-1", 30000, "COP")

    assert error.value.code == CODES.CONFLICT_IDEMPOTENCIA
    assert len(pasarela.calls) == 1, "un conflicto no puede llegar a la pasarela"


async def test_un_cobro_fallido_no_deja_pago_registrado(servicio, sesiones, pasarela):
    pasarela.error = ServiceError(CODES.DEPENDENCY_UNAVAILABLE, "la pasarela no esta disponible")

    with pytest.raises(ServiceError) as error:
        await servicio.cobrar("saga-1", 25000, "COP")

    assert error.value.code == CODES.DEPENDENCY_UNAVAILABLE
    assert sesiones.session.filas(Pago) == [], "no puede quedar un pago a medias"
    registros = sesiones.session.filas(IdempotencyRecord)
    assert all(registro.estado != "COMPLETADO" for registro in registros)


async def test_tras_un_cobro_fallido_se_puede_reintentar(servicio, sesiones, pasarela):
    pasarela.error = ServiceError(CODES.DEPENDENCY_UNAVAILABLE, "la pasarela no esta disponible")
    with pytest.raises(ServiceError):
        await servicio.cobrar("saga-1", 25000, "COP")

    pasarela.error = None
    resultado = await servicio.cobrar("saga-1", 25000, "COP")

    assert resultado["pago"]["estado"] == CONFIRMADO
    assert len(sesiones.session.filas(Pago)) == 1


async def test_un_cobro_con_el_circuito_abierto_no_deja_pago_registrado(servicio, sesiones, pasarela):
    pasarela.error = ServiceError(CODES.CIRCUIT_OPEN, "degradado: pasarela no disponible")

    with pytest.raises(ServiceError) as error:
        await servicio.cobrar("saga-1", 25000, "COP")

    assert error.value.code == CODES.CIRCUIT_OPEN
    assert sesiones.session.filas(Pago) == []


async def test_la_clave_de_idempotencia_se_guarda(servicio, sesiones):
    await servicio.cobrar("saga-1", 25000, "COP")

    registros = sesiones.session.filas(IdempotencyRecord)
    assert len(registros) == 1
    assert registros[0].clave == "saga-1:COBRAR"
    assert registros[0].estado == "COMPLETADO"


async def test_la_transaccion_no_queda_abierta_durante_la_llamada_a_la_pasarela(sesiones):
    """El cobro no puede retener la base de datos mientras la pasarela tarda.

    Si la transaccion quedara abierta, una pasarela lenta bloquearia al
    resto de los cobros (y el bulkhead no podria rechazar el exceso).
    """
    commits_al_llamar = []

    class PasarelaQueMireLaSesion(FakePasarela):
        async def cobrar(self, monto_centavos, idempotency_key):
            commits_al_llamar.append(sesiones.session.commits)
            return await super().cobrar(monto_centavos, idempotency_key)

    servicio = PagosService(sesiones, PasarelaQueMireLaSesion())

    await servicio.cobrar("saga-1", 25000, "COP")

    assert commits_al_llamar == [1], "la reserva se cierra antes de llamar a la pasarela"
    assert sesiones.session.commits == 2, "la escritura del pago es la segunda transaccion"


async def test_consultar_pago_devuelve_el_pago(servicio):
    await servicio.cobrar("saga-1", 25000, "COP")

    pago = servicio.consultar_pago("saga-1")

    assert pago["estado"] == CONFIRMADO
    assert pago["monto_centavos"] == 25000


async def test_consultar_un_pago_inexistente_es_not_found(servicio):
    with pytest.raises(ServiceError) as error:
        servicio.consultar_pago("saga-9")

    assert error.value.code == CODES.NOT_FOUND


# -------------------------------------------------------------- reembolso --
async def test_reembolsar_marca_el_pago_como_reembolsado(servicio, pasarela):
    await servicio.cobrar("saga-1", 25000, "COP")

    resultado = await servicio.reembolsar("saga-1")

    assert resultado["idempotente"] is False
    assert resultado["pago"]["estado"] == REEMBOLSADO
    assert ("reembolsar", "pgw_falso") in pasarela.calls


async def test_reembolsar_es_idempotente(servicio, pasarela):
    await servicio.cobrar("saga-1", 25000, "COP")

    primera = await servicio.reembolsar("saga-1")
    segunda = await servicio.reembolsar("saga-1")

    assert segunda["idempotente"] is True
    assert segunda["pago"] == primera["pago"]
    reembolsos = [llamada for llamada in pasarela.calls if llamada[0] == "reembolsar"]
    assert len(reembolsos) == 1, "un comando repetido no puede reembolsar dos veces"


async def test_reembolsar_sin_pago_es_not_found(servicio):
    with pytest.raises(ServiceError) as error:
        await servicio.reembolsar("saga-9")

    assert error.value.code == CODES.NOT_FOUND


async def test_un_reembolso_fallido_no_deja_el_pago_reembolsado(servicio, sesiones, pasarela):
    await servicio.cobrar("saga-1", 25000, "COP")
    pasarela.error = ServiceError(CODES.DEPENDENCY_UNAVAILABLE, "la pasarela no esta disponible")

    with pytest.raises(ServiceError):
        await servicio.reembolsar("saga-1")

    assert servicio.consultar_pago("saga-1")["estado"] == CONFIRMADO
    registros = [r for r in sesiones.session.filas(IdempotencyRecord) if r.clave.endswith("REEMBOLSAR")]
    assert all(registro.estado != "COMPLETADO" for registro in registros)


# ------------------------------------------------------------ observabilidad --
async def test_el_estado_del_circuito_lo_expone_el_servicio(servicio):
    estado = servicio.estado_circuito()

    assert estado["estado"] == "CLOSED"
    assert estado["fallback"] == ""


def test_el_identificador_del_pago_depende_de_la_saga():
    assert payment_id_for("saga-1") == payment_id_for("saga-1")
    assert payment_id_for("saga-1") != payment_id_for("saga-2")


def test_los_metodos_de_pago_estan_disponibles(servicio):
    metodos = servicio.listar_metodos_pago()

    assert metodos
    assert all("metodo" in metodo and "descripcion" in metodo for metodo in metodos)
