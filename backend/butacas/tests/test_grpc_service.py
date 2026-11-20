"""Tests of the gRPC adapter of the `butacas` service.

They check that the domain answers reach the caller as the protobuf messages
of `proto/butacas.proto` and that the domain errors reach the caller as a
gRPC status plus a machine readable code.
"""

from __future__ import annotations

import json

import grpc
import pytest

import butacas_pb2
from common.errors import CODES, ServiceError

from ..grpc_service import ButacasServicer


class AbortError(Exception):
    """Raised by the fake context when the servicer aborts the call."""


class FakeContext:
    """Fake of the gRPC context: records the abort of the servicer."""

    def __init__(self) -> None:
        self.code = None
        self.details = None

    async def abort(self, code, details):
        self.code = code
        self.details = details
        raise AbortError(code, details)


@pytest.fixture()
def servicer(servicio):
    return ButacasServicer(servicio)


async def test_crear_evento_devuelve_el_evento(servicer):
    respuesta = await servicer.CrearEvento(
        butacas_pb2.CrearEventoRequest(
            nombre="Concierto de prueba", lugar="Teatro de prueba", fecha="2026-12-01T20:00:00Z"
        ),
        FakeContext(),
    )

    assert respuesta.evento_id
    assert respuesta.nombre == "Concierto de prueba"
    assert respuesta.lugar == "Teatro de prueba"
    assert respuesta.fecha.startswith("2026-12-01")


async def test_crear_seccion_devuelve_la_seccion_con_sus_butacas(servicer):
    evento = await servicer.CrearEvento(
        butacas_pb2.CrearEventoRequest(
            nombre="Concierto", lugar="Teatro", fecha="2026-12-01T20:00:00Z"
        ),
        FakeContext(),
    )

    seccion = await servicer.CrearSeccion(
        butacas_pb2.CrearSeccionRequest(
            evento_id=evento.evento_id, nombre="Platea", capacidad_total=16, precio_centavos=25000
        ),
        FakeContext(),
    )

    assert seccion.seccion_id
    assert seccion.capacidad_total == 16
    assert seccion.cantidad_disponible == 16
    assert seccion.precio_centavos == 25000


async def test_listar_eventos_y_secciones_y_el_plano_de_la_sala(servicer):
    evento = await servicer.CrearEvento(
        butacas_pb2.CrearEventoRequest(
            nombre="Concierto", lugar="Teatro", fecha="2026-12-01T20:00:00Z"
        ),
        FakeContext(),
    )
    seccion = await servicer.CrearSeccion(
        butacas_pb2.CrearSeccionRequest(
            evento_id=evento.evento_id, nombre="Platea", capacidad_total=16, precio_centavos=25000
        ),
        FakeContext(),
    )

    eventos = await servicer.ListarEventos(butacas_pb2.ListarEventosRequest(), FakeContext())
    secciones = await servicer.ListarSecciones(
        butacas_pb2.ListarSeccionesRequest(evento_id=evento.evento_id), FakeContext()
    )
    plano = await servicer.ListarButacas(
        butacas_pb2.ListarButacasRequest(seccion_id=seccion.seccion_id), FakeContext()
    )

    assert [e.evento_id for e in eventos.eventos] == [evento.evento_id]
    assert [s.seccion_id for s in secciones.secciones] == [seccion.seccion_id]
    assert secciones.secciones[0].cantidad_disponible == 16
    assert len(plano.butacas) == 16
    assert plano.butacas[0].fila == "A"
    assert plano.butacas[0].numero == 1
    assert plano.butacas[0].estado == "LIBRE"


async def test_reservar_devuelve_la_reserva_y_sus_butacas(servicer):
    evento = await servicer.CrearEvento(
        butacas_pb2.CrearEventoRequest(
            nombre="Concierto", lugar="Teatro", fecha="2026-12-01T20:00:00Z"
        ),
        FakeContext(),
    )
    seccion = await servicer.CrearSeccion(
        butacas_pb2.CrearSeccionRequest(
            evento_id=evento.evento_id, nombre="Platea", capacidad_total=16, precio_centavos=25000
        ),
        FakeContext(),
    )
    plano = await servicer.ListarButacas(
        butacas_pb2.ListarButacasRequest(seccion_id=seccion.seccion_id), FakeContext()
    )
    elegidas = [b.butaca_id for b in plano.butacas[:2]]

    respuesta = await servicer.Reservar(
        butacas_pb2.ReservarRequest(
            saga_id="saga-1",
            evento_id=evento.evento_id,
            seccion_id=seccion.seccion_id,
            butaca_ids=elegidas,
            idempotency_key="saga-1:RESERVAR",
        ),
        FakeContext(),
    )

    assert respuesta.estado == "ACTIVA"
    assert respuesta.reserva.saga_id == "saga-1"
    assert respuesta.reserva.precio_centavos == 25000
    assert len(respuesta.butacas) == 2
    assert all(b.estado == "RESERVADA" for b in respuesta.butacas)
    assert all(b.saga_id == "saga-1" for b in respuesta.butacas)


async def test_un_comando_repetido_devuelve_la_misma_reserva(servicer):
    evento = await servicer.CrearEvento(
        butacas_pb2.CrearEventoRequest(
            nombre="Concierto", lugar="Teatro", fecha="2026-12-01T20:00:00Z"
        ),
        FakeContext(),
    )
    seccion = await servicer.CrearSeccion(
        butacas_pb2.CrearSeccionRequest(
            evento_id=evento.evento_id, nombre="Platea", capacidad_total=16, precio_centavos=25000
        ),
        FakeContext(),
    )
    plano = await servicer.ListarButacas(
        butacas_pb2.ListarButacasRequest(seccion_id=seccion.seccion_id), FakeContext()
    )
    peticion = butacas_pb2.ReservarRequest(
        saga_id="saga-1",
        evento_id=evento.evento_id,
        seccion_id=seccion.seccion_id,
        butaca_ids=[b.butaca_id for b in plano.butacas[:2]],
        idempotency_key="saga-1:RESERVAR",
    )

    primera = await servicer.Reservar(peticion, FakeContext())
    segunda = await servicer.Reservar(peticion, FakeContext())

    assert primera.reserva.reserva_id == segunda.reserva.reserva_id


async def test_una_butaca_ya_reservada_se_traduce_a_already_exists(servicer):
    evento = await servicer.CrearEvento(
        butacas_pb2.CrearEventoRequest(
            nombre="Concierto", lugar="Teatro", fecha="2026-12-01T20:00:00Z"
        ),
        FakeContext(),
    )
    seccion = await servicer.CrearSeccion(
        butacas_pb2.CrearSeccionRequest(
            evento_id=evento.evento_id, nombre="Platea", capacidad_total=16, precio_centavos=25000
        ),
        FakeContext(),
    )
    plano = await servicer.ListarButacas(
        butacas_pb2.ListarButacasRequest(seccion_id=seccion.seccion_id), FakeContext()
    )
    butaca_id = plano.butacas[0].butaca_id
    await servicer.Reservar(
        butacas_pb2.ReservarRequest(
            saga_id="saga-1",
            evento_id=evento.evento_id,
            seccion_id=seccion.seccion_id,
            butaca_ids=[butaca_id],
            idempotency_key="saga-1:RESERVAR",
        ),
        FakeContext(),
    )

    contexto = FakeContext()
    with pytest.raises(AbortError):
        await servicer.Reservar(
            butacas_pb2.ReservarRequest(
                saga_id="saga-2",
                evento_id=evento.evento_id,
                seccion_id=seccion.seccion_id,
                butaca_ids=[butaca_id],
                idempotency_key="saga-2:RESERVAR",
            ),
            contexto,
        )

    assert contexto.code == grpc.StatusCode.ALREADY_EXISTS
    cuerpo = json.loads(contexto.details)
    assert cuerpo["code"] == CODES.ALREADY_EXISTS


async def test_liberar_y_confirmar_venta(servicer):
    evento = await servicer.CrearEvento(
        butacas_pb2.CrearEventoRequest(
            nombre="Concierto", lugar="Teatro", fecha="2026-12-01T20:00:00Z"
        ),
        FakeContext(),
    )
    seccion = await servicer.CrearSeccion(
        butacas_pb2.CrearSeccionRequest(
            evento_id=evento.evento_id, nombre="Platea", capacidad_total=16, precio_centavos=25000
        ),
        FakeContext(),
    )
    plano = await servicer.ListarButacas(
        butacas_pb2.ListarButacasRequest(seccion_id=seccion.seccion_id), FakeContext()
    )
    await servicer.Reservar(
        butacas_pb2.ReservarRequest(
            saga_id="saga-1",
            evento_id=evento.evento_id,
            seccion_id=seccion.seccion_id,
            butaca_ids=[b.butaca_id for b in plano.butacas[:2]],
            idempotency_key="saga-1:RESERVAR",
        ),
        FakeContext(),
    )

    liberado = await servicer.Liberar(
        butacas_pb2.LiberarRequest(saga_id="saga-1", idempotency_key="saga-1:LIBERAR"), FakeContext()
    )
    assert liberado.estado == "LIBERADA"
    assert liberado.butacas_liberadas == 2

    await servicer.Reservar(
        butacas_pb2.ReservarRequest(
            saga_id="saga-1",
            evento_id=evento.evento_id,
            seccion_id=seccion.seccion_id,
            butaca_ids=[b.butaca_id for b in plano.butacas[:2]],
            idempotency_key="saga-1:RESERVAR-2",
        ),
        FakeContext(),
    )
    confirmada = await servicer.ConfirmarVenta(
        butacas_pb2.ConfirmarVentaRequest(saga_id="saga-1"), FakeContext()
    )
    assert confirmada.confirmada is True
    assert confirmada.estado == "VENDIDA"
    assert confirmada.butacas_confirmadas == 2


async def test_consultar_reserva(servicer):
    evento = await servicer.CrearEvento(
        butacas_pb2.CrearEventoRequest(
            nombre="Concierto", lugar="Teatro", fecha="2026-12-01T20:00:00Z"
        ),
        FakeContext(),
    )
    seccion = await servicer.CrearSeccion(
        butacas_pb2.CrearSeccionRequest(
            evento_id=evento.evento_id, nombre="Platea", capacidad_total=16, precio_centavos=25000
        ),
        FakeContext(),
    )
    plano = await servicer.ListarButacas(
        butacas_pb2.ListarButacasRequest(seccion_id=seccion.seccion_id), FakeContext()
    )
    await servicer.Reservar(
        butacas_pb2.ReservarRequest(
            saga_id="saga-1",
            evento_id=evento.evento_id,
            seccion_id=seccion.seccion_id,
            butaca_ids=[b.butaca_id for b in plano.butacas[:1]],
            idempotency_key="saga-1:RESERVAR",
        ),
        FakeContext(),
    )

    respuesta = await servicer.ConsultarReserva(
        butacas_pb2.ConsultarReservaRequest(saga_id="saga-1"), FakeContext()
    )

    assert respuesta.estado == "ACTIVA"
    assert respuesta.reserva.saga_id == "saga-1"
    assert len(respuesta.butacas) == 1


async def test_el_crud_de_eventos_y_secciones(servicer):
    evento = await servicer.CrearEvento(
        butacas_pb2.CrearEventoRequest(
            nombre="Concierto", lugar="Teatro", fecha="2026-12-01T20:00:00Z"
        ),
        FakeContext(),
    )
    actualizado = await servicer.ActualizarEvento(
        butacas_pb2.ActualizarEventoRequest(
            evento_id=evento.evento_id,
            nombre="Concierto 2",
            lugar="Teatro 2",
            fecha="2026-12-02T20:00:00Z",
        ),
        FakeContext(),
    )
    assert actualizado.nombre == "Concierto 2"

    seccion = await servicer.CrearSeccion(
        butacas_pb2.CrearSeccionRequest(
            evento_id=evento.evento_id, nombre="Platea", capacidad_total=8, precio_centavos=1000
        ),
        FakeContext(),
    )
    seccion_actualizada = await servicer.ActualizarSeccion(
        butacas_pb2.ActualizarSeccionRequest(
            seccion_id=seccion.seccion_id, nombre="Platea A", capacidad_total=8, precio_centavos=2000
        ),
        FakeContext(),
    )
    assert seccion_actualizada.precio_centavos == 2000

    eliminado = await servicer.EliminarSeccion(
        butacas_pb2.EliminarSeccionRequest(seccion_id=seccion.seccion_id), FakeContext()
    )
    assert eliminado.eliminado is True

    eliminado_evento = await servicer.EliminarEvento(
        butacas_pb2.EliminarEventoRequest(evento_id=evento.evento_id), FakeContext()
    )
    assert eliminado_evento.eliminado is True


async def test_un_evento_inexistente_se_traduce_a_not_found(servicer):
    contexto = FakeContext()

    with pytest.raises(AbortError):
        await servicer.ActualizarEvento(
            butacas_pb2.ActualizarEventoRequest(
                evento_id="ev-inexistente", nombre="X", lugar="Y", fecha="2026-12-01T20:00:00Z"
            ),
            contexto,
        )

    assert contexto.code == grpc.StatusCode.NOT_FOUND
    assert json.loads(contexto.details)["code"] == CODES.NOT_FOUND
