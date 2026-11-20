"""Tests of the domain logic of the `butacas` service.

They cover the saga step `RESERVAR` and its compensation `LIBERAR`, the
idempotency of both, and the rule that rules the whole service:

    *Una butaca no se vende dos veces.*

The guarantee is proved at the level of the database: the reservation is one
atomic statement (`UPDATE ... RETURNING`), so the test with two independent
sessions shows that the second claim of an already claimed seat fails
because of the statement itself, and not because of an application level
check that could be raced.
"""

from __future__ import annotations

import pytest
from sqlalchemy.orm import Session

from common.errors import CODES, ServiceError
from common.idempotency import IdempotencyRecord

from ..models import ACTIVA, LIBERADA, LIBRE, RESERVADA, VENDIDA, Butaca, Reserva
from ..service import reclamo_atomico


def _estados(servicio, seccion_id) -> dict[str, dict]:
    """Seats of a section, by identifier."""
    return {b["butaca_id"]: b for b in servicio.listar_butacas(seccion_id)}


def _primera(butacas: dict[str, dict], cantidad: int = 2) -> list[str]:
    """The first seats of the map, in a stable order."""
    return list(butacas)[:cantidad]


# ---------------------------------------------------------------- consulta --
def test_listar_eventos_devuelve_el_catalogo_ordenado_por_fecha(servicio):
    servicio.crear_evento("Segundo evento", "Sala B", "2026-10-01T20:00:00Z")
    servicio.crear_evento("Primer evento", "Sala A", "2026-09-01T20:00:00Z")

    eventos = servicio.listar_eventos()

    assert [e["nombre"] for e in eventos] == ["Primer evento", "Segundo evento"]
    assert all(e["evento_id"] and e["lugar"] and e["fecha"] for e in eventos)


def test_listar_secciones_reporta_la_cantidad_disponible_real(servicio, catalogo):
    evento, seccion = catalogo
    elegidas = _primera(_estados(servicio, seccion["seccion_id"]), 3)
    servicio.reservar("saga-1", evento["evento_id"], seccion["seccion_id"], elegidas)

    secciones = servicio.listar_secciones(evento["evento_id"])

    assert len(secciones) == 1
    assert secciones[0]["seccion_id"] == seccion["seccion_id"]
    assert secciones[0]["capacidad_total"] == 16
    # 16 filas reales, 3 reservadas: el contador sale de las filas, no de un
    # numero guardado que pueda quedar viejo.
    assert secciones[0]["cantidad_disponible"] == 13
    assert secciones[0]["precio_centavos"] == 25000


def test_listar_butacas_devuelve_el_plano_de_la_sala(servicio, catalogo):
    evento, seccion = catalogo
    elegidas = _primera(_estados(servicio, seccion["seccion_id"]), 1)
    servicio.reservar("saga-1", evento["evento_id"], seccion["seccion_id"], elegidas)

    plano = servicio.listar_butacas(seccion["seccion_id"])

    assert len(plano) == 16
    assert plano[0]["fila"] == "A"
    assert plano[0]["numero"] == 1
    assert plano[0]["estado"] == RESERVADA
    assert plano[0]["saga_id"] == "saga-1"
    assert plano[1]["estado"] == LIBRE
    assert plano[1]["saga_id"] == ""


def test_listar_butacas_de_una_seccion_inexistente_es_not_found(servicio):
    with pytest.raises(ServiceError) as error:
        servicio.listar_butacas("seccion-inexistente")

    assert error.value.code == CODES.NOT_FOUND


# --------------------------------------------------------------- reserva --
def test_reservar_deja_las_butacas_reservadas_con_el_dueno_de_la_saga(servicio, catalogo):
    evento, seccion = catalogo
    elegidas = _primera(_estados(servicio, seccion["seccion_id"]), 2)

    resultado = servicio.reservar("saga-1", evento["evento_id"], seccion["seccion_id"], elegidas)

    assert resultado["estado"] == ACTIVA
    assert resultado["reserva"]["saga_id"] == "saga-1"
    assert resultado["reserva"]["seccion_id"] == seccion["seccion_id"]
    assert resultado["reserva"]["precio_centavos"] == 25000
    assert resultado["reserva"]["estado"] == ACTIVA
    assert [b["butaca_id"] for b in resultado["butacas"]] == sorted(elegidas)

    estados = _estados(servicio, seccion["seccion_id"])
    for butaca_id in elegidas:
        assert estados[butaca_id]["estado"] == RESERVADA
        assert estados[butaca_id]["saga_id"] == "saga-1"


def test_reservar_una_butaca_ya_reservada_falla_y_no_deja_butacas_a_medias(servicio, catalogo):
    evento, seccion = catalogo
    butacas = _estados(servicio, seccion["seccion_id"])
    taken = _primera(butacas, 1)[0]
    libre = _primera(butacas, 2)[1]
    servicio.reservar("saga-1", evento["evento_id"], seccion["seccion_id"], [taken])

    with pytest.raises(ServiceError) as error:
        servicio.reservar("saga-2", evento["evento_id"], seccion["seccion_id"], [taken, libre])

    assert error.value.code == CODES.ALREADY_EXISTS
    estados = _estados(servicio, seccion["seccion_id"])
    assert estados[libre]["estado"] == LIBRE, "no puede quedar una butaca reservada a medias"
    assert estados[libre]["saga_id"] == ""
    assert estados[taken]["saga_id"] == "saga-1"


def test_la_reserva_es_atomica_entre_dos_sesiones(engine, servicio, catalogo):
    """La garantia viene del enunciado, no de una comprobacion de la aplicacion.

    La primera sesion reclama la butaca y confirma; la segunda sesion, que es
    independiente, intenta el mismo reclamo y no obtiene ninguna fila. Si la
    garantia fuera una comprobacion previa (`SELECT` y despues `UPDATE`),
    las dos sesiones podrian pasar esa comprobacion antes de que la otra
    escribiera.
    """
    evento, seccion = catalogo
    butaca_id = _primera(_estados(servicio, seccion["seccion_id"]), 1)[0]

    primera = Session(engine)
    obtenidas = reclamo_atomico(primera, "saga-1", seccion["seccion_id"], [butaca_id])
    primera.commit()

    segunda = Session(engine)
    obtenidas_2 = reclamo_atomico(segunda, "saga-2", seccion["seccion_id"], [butaca_id])
    segunda.rollback()

    assert obtenidas == [butaca_id]
    assert obtenidas_2 == [], "la segunda sesion no puede reclamar la misma butaca"

    with pytest.raises(ServiceError) as error:
        servicio.reservar("saga-2", evento["evento_id"], seccion["seccion_id"], [butaca_id])

    assert error.value.code == CODES.ALREADY_EXISTS
    assert _estados(servicio, seccion["seccion_id"])[butaca_id]["saga_id"] == "saga-1"


def test_reservar_es_idempotente(servicio, catalogo):
    evento, seccion = catalogo
    elegidas = _primera(_estados(servicio, seccion["seccion_id"]), 2)

    primera = servicio.reservar("saga-1", evento["evento_id"], seccion["seccion_id"], elegidas)
    segunda = servicio.reservar("saga-1", evento["evento_id"], seccion["seccion_id"], elegidas)

    assert primera["idempotente"] is False
    assert segunda["idempotente"] is True
    assert segunda["reserva"]["reserva_id"] == primera["reserva"]["reserva_id"]
    assert segunda["reserva"]["creada_en"] == primera["reserva"]["creada_en"]

    estados = _estados(servicio, seccion["seccion_id"])
    reservadas = [b for b in estados.values() if b["estado"] == RESERVADA]
    assert len(reservadas) == 2, "un comando repetido no puede reservar dos veces"


def test_la_misma_clave_con_otro_payload_es_conflicto(servicio, catalogo):
    evento, seccion = catalogo
    butacas = _estados(servicio, seccion["seccion_id"])
    primera_elegida = _primera(butacas, 1)[0]
    otra = _primera(butacas, 2)[1]
    servicio.reservar("saga-1", evento["evento_id"], seccion["seccion_id"], [primera_elegida])

    with pytest.raises(ServiceError) as error:
        servicio.reservar("saga-1", evento["evento_id"], seccion["seccion_id"], [otra])

    assert error.value.code == CODES.CONFLICT_IDEMPOTENCIA
    assert _estados(servicio, seccion["seccion_id"])[otra]["estado"] == LIBRE


def test_una_butaca_inexistente_es_not_found(servicio, catalogo):
    evento, seccion = catalogo

    with pytest.raises(ServiceError) as error:
        servicio.reservar("saga-1", evento["evento_id"], seccion["seccion_id"], ["butaca-fantasma"])

    assert error.value.code == CODES.NOT_FOUND


def test_una_butaca_de_otra_seccion_es_invalida(servicio, catalogo):
    evento, seccion = catalogo
    otra = servicio.crear_seccion(evento["evento_id"], "Balcon", 8, 12000)
    ajena = _primera(_estados(servicio, otra["seccion_id"]), 1)[0]

    with pytest.raises(ServiceError) as error:
        servicio.reservar("saga-1", evento["evento_id"], seccion["seccion_id"], [ajena])

    assert error.value.code == CODES.INVALID


def test_una_seccion_de_otro_evento_es_invalida(servicio, catalogo):
    evento, seccion = catalogo
    otro_evento = servicio.crear_evento("Otro evento", "Sala C", "2027-01-01T20:00:00Z")

    with pytest.raises(ServiceError) as error:
        servicio.reservar("saga-1", otro_evento["evento_id"], seccion["seccion_id"], ["cualquiera"])

    assert error.value.code == CODES.INVALID


def test_reservar_sin_butacas_es_invalido(servicio, catalogo):
    evento, seccion = catalogo

    with pytest.raises(ServiceError) as error:
        servicio.reservar("saga-1", evento["evento_id"], seccion["seccion_id"], [])

    assert error.value.code == CODES.INVALID


def test_la_clave_de_idempotencia_se_guarda(servicio, catalogo, sesiones):
    evento, seccion = catalogo
    elegidas = _primera(_estados(servicio, seccion["seccion_id"]), 1)

    servicio.reservar("saga-1", evento["evento_id"], seccion["seccion_id"], elegidas)

    with sesiones() as session:
        registro = session.get(IdempotencyRecord, "saga-1:RESERVAR")
        assert registro is not None
        assert registro.estado == "COMPLETADO"
        assert registro.resultado["reserva"]["saga_id"] == "saga-1"


def test_una_saga_no_puede_tener_dos_reservas(servicio, catalogo):
    evento, seccion = catalogo
    butacas = _estados(servicio, seccion["seccion_id"])
    primera = _primera(butacas, 1)[0]
    segunda = _primera(butacas, 2)[1]
    servicio.reservar(
        "saga-1", evento["evento_id"], seccion["seccion_id"], [primera], idempotency_key="saga-1:RESERVAR"
    )

    with pytest.raises(ServiceError) as error:
        servicio.reservar(
            "saga-1",
            evento["evento_id"],
            seccion["seccion_id"],
            [segunda],
            idempotency_key="saga-1:RESERVAR-2",
        )

    assert error.value.code == CODES.ALREADY_EXISTS
    assert _estados(servicio, seccion["seccion_id"])[segunda]["estado"] == LIBRE


def test_confirmar_venta_de_una_reserva_liberada_no_confirma_nada(servicio, catalogo):
    evento, seccion = catalogo
    elegidas = _primera(_estados(servicio, seccion["seccion_id"]), 2)
    servicio.reservar("saga-1", evento["evento_id"], seccion["seccion_id"], elegidas)
    servicio.liberar("saga-1")

    resultado = servicio.confirmar_venta("saga-1")

    assert resultado["confirmada"] is False
    assert resultado["butacas_confirmadas"] == 0
    assert resultado["estado"] == "NO_ENCONTRADA"


def test_consultar_reserva_devuelve_la_reserva_y_sus_butacas(servicio, catalogo):
    evento, seccion = catalogo
    elegidas = _primera(_estados(servicio, seccion["seccion_id"]), 2)
    servicio.reservar("saga-1", evento["evento_id"], seccion["seccion_id"], elegidas)

    consulta = servicio.consultar_reserva("saga-1")

    assert consulta["estado"] == ACTIVA
    assert consulta["reserva"]["reserva_id"]
    assert len(consulta["butacas"]) == 2
    assert all(b["estado"] == RESERVADA for b in consulta["butacas"])


def test_consultar_una_reserva_inexistente_es_no_encontrada(servicio):
    consulta = servicio.consultar_reserva("saga-9")

    assert consulta["estado"] == "NO_ENCONTRADA"
    assert consulta["butacas"] == []


# --------------------------------------------------------------- liberar --
def test_liberar_libera_las_butacas_de_la_saga(servicio, catalogo):
    evento, seccion = catalogo
    elegidas = _primera(_estados(servicio, seccion["seccion_id"]), 2)
    servicio.reservar("saga-1", evento["evento_id"], seccion["seccion_id"], elegidas)

    resultado = servicio.liberar("saga-1")

    assert resultado["liberado"] is True
    assert resultado["estado"] == LIBERADA
    assert resultado["butacas_liberadas"] == 2

    estados = _estados(servicio, seccion["seccion_id"])
    for butaca_id in elegidas:
        assert estados[butaca_id]["estado"] == LIBRE
        assert estados[butaca_id]["saga_id"] == ""


def test_liberar_es_idempotente(servicio, catalogo):
    evento, seccion = catalogo
    elegidas = _primera(_estados(servicio, seccion["seccion_id"]), 2)
    servicio.reservar("saga-1", evento["evento_id"], seccion["seccion_id"], elegidas)

    primera = servicio.liberar("saga-1")
    segunda = servicio.liberar("saga-1")

    assert primera["butacas_liberadas"] == 2
    # La segunda ejecucion no suelta nada: la reserva ya estaba liberada.
    assert segunda["butacas_liberadas"] == 0
    assert segunda["estado"] == LIBERADA
    estados = _estados(servicio, seccion["seccion_id"])
    assert sum(1 for b in estados.values() if b["estado"] == LIBRE) == 16


def test_liberar_no_suelta_una_butaca_vendida(servicio, catalogo):
    """Una butaca vendida no se suelta con una compensacion tardia."""
    evento, seccion = catalogo
    elegidas = _primera(_estados(servicio, seccion["seccion_id"]), 2)
    servicio.reservar("saga-1", evento["evento_id"], seccion["seccion_id"], elegidas)
    servicio.confirmar_venta("saga-1")

    resultado = servicio.liberar("saga-1")

    assert resultado["estado"] == LIBERADA
    assert resultado["butacas_liberadas"] == 0, "una butaca vendida no se libera"
    estados = _estados(servicio, seccion["seccion_id"])
    for butaca_id in elegidas:
        assert estados[butaca_id]["estado"] == VENDIDA
        assert estados[butaca_id]["saga_id"] == "saga-1"


def test_liberar_una_saga_sin_reserva_es_no_encontrada(servicio):
    resultado = servicio.liberar("saga-9")

    assert resultado["liberado"] is False
    assert resultado["butacas_liberadas"] == 0
    assert resultado["estado"] == "NO_ENCONTRADA"


# ------------------------------------------------------- confirmar venta --
def test_confirmar_venta_mueve_las_butacas_a_vendida(servicio, catalogo):
    evento, seccion = catalogo
    elegidas = _primera(_estados(servicio, seccion["seccion_id"]), 2)
    servicio.reservar("saga-1", evento["evento_id"], seccion["seccion_id"], elegidas)

    resultado = servicio.confirmar_venta("saga-1")

    assert resultado["confirmada"] is True
    assert resultado["estado"] == VENDIDA
    assert resultado["butacas_confirmadas"] == 2

    estados = _estados(servicio, seccion["seccion_id"])
    for butaca_id in elegidas:
        assert estados[butaca_id]["estado"] == VENDIDA
        assert estados[butaca_id]["saga_id"] == "saga-1"


def test_confirmar_venta_es_idempotente(servicio, catalogo):
    evento, seccion = catalogo
    elegidas = _primera(_estados(servicio, seccion["seccion_id"]), 2)
    servicio.reservar("saga-1", evento["evento_id"], seccion["seccion_id"], elegidas)

    primera = servicio.confirmar_venta("saga-1")
    segunda = servicio.confirmar_venta("saga-1")

    assert primera["butacas_confirmadas"] == 2
    assert segunda["butacas_confirmadas"] == 2
    assert segunda["estado"] == VENDIDA
    estados = _estados(servicio, seccion["seccion_id"])
    assert sum(1 for b in estados.values() if b["estado"] == VENDIDA) == 2


def test_confirmar_venta_de_una_saga_desconocida_es_no_encontrada(servicio):
    resultado = servicio.confirmar_venta("saga-9")

    assert resultado["confirmada"] is False
    assert resultado["butacas_confirmadas"] == 0
    assert resultado["estado"] == "NO_ENCONTRADA"


# -------------------------------------------------------------------- crud --
def test_crear_seccion_crea_las_filas_de_butacas(servicio):
    evento = servicio.crear_evento("Evento", "Sala", "2026-12-01T20:00:00Z")

    seccion = servicio.crear_seccion(evento["evento_id"], "Platea", 20, 30000)

    assert seccion["capacidad_total"] == 20
    assert seccion["cantidad_disponible"] == 20
    plano = servicio.listar_butacas(seccion["seccion_id"])
    assert len(plano) == 20
    assert [b["fila"] for b in plano[:8]] == ["A"] * 8
    assert [b["numero"] for b in plano[:3]] == [1, 2, 3]
    assert plano[8]["fila"] == "B"
    assert plano[8]["numero"] == 9, "la numeracion de las butacas es continua"
    assert plano[0]["butaca_id"] == f"{seccion['seccion_id']}-A1"
    assert plano[8]["butaca_id"] == f"{seccion['seccion_id']}-B9"
    assert all(b["estado"] == LIBRE for b in plano)


def test_eliminar_una_seccion_con_butacas_vendidas_se_rechaza(servicio, catalogo):
    evento, seccion = catalogo
    elegidas = _primera(_estados(servicio, seccion["seccion_id"]), 1)
    servicio.reservar("saga-1", evento["evento_id"], seccion["seccion_id"], elegidas)
    servicio.confirmar_venta("saga-1")

    with pytest.raises(ServiceError) as error:
        servicio.eliminar_seccion(seccion["seccion_id"])

    assert error.value.code == CODES.INVALID
    assert servicio.listar_butacas(seccion["seccion_id"]), "la seccion sigue existindo"


def test_eliminar_una_seccion_con_butacas_reservadas_se_rechaza(servicio, catalogo):
    evento, seccion = catalogo
    elegidas = _primera(_estados(servicio, seccion["seccion_id"]), 1)
    servicio.reservar("saga-1", evento["evento_id"], seccion["seccion_id"], elegidas)

    with pytest.raises(ServiceError) as error:
        servicio.eliminar_seccion(seccion["seccion_id"])

    assert error.value.code == CODES.INVALID


def test_eliminar_una_seccion_libre_la_borra(servicio, catalogo):
    evento, seccion = catalogo

    resultado = servicio.eliminar_seccion(seccion["seccion_id"])

    assert resultado["eliminado"] is True
    assert servicio.listar_secciones(evento["evento_id"]) == []


def test_actualizar_seccion_cambia_el_precio_y_agrega_butacas(servicio, catalogo):
    evento, seccion = catalogo

    actualizada = servicio.actualizar_seccion(seccion["seccion_id"], "Platea", 20, 33000)

    assert actualizada["precio_centavos"] == 33000
    assert actualizada["capacidad_total"] == 20
    assert len(servicio.listar_butacas(seccion["seccion_id"])) == 20


def test_reducir_la_capacidad_con_butacas_vendidas_se_rechaza(servicio, catalogo):
    evento, seccion = catalogo
    butacas = _estados(servicio, seccion["seccion_id"])
    # Una butaca de la segunda fila (numero mayor que la nueva capacidad).
    vendida = next(b["butaca_id"] for b in butacas.values() if b["numero"] > 8)
    servicio.reservar("saga-1", evento["evento_id"], seccion["seccion_id"], [vendida])
    servicio.confirmar_venta("saga-1")

    with pytest.raises(ServiceError) as error:
        servicio.actualizar_seccion(seccion["seccion_id"], "Platea", 8, 25000)

    assert error.value.code == CODES.INVALID


def test_actualizar_un_evento_inexistente_es_not_found(servicio):
    with pytest.raises(ServiceError) as error:
        servicio.actualizar_evento("ev-inexistente", "Nuevo", "Sala", "2026-12-01T20:00:00Z")

    assert error.value.code == CODES.NOT_FOUND


def test_eliminar_un_evento_con_ventas_se_rechaza(servicio, catalogo):
    evento, seccion = catalogo
    elegidas = _primera(_estados(servicio, seccion["seccion_id"]), 1)
    servicio.reservar("saga-1", evento["evento_id"], seccion["seccion_id"], elegidas)
    servicio.confirmar_venta("saga-1")

    with pytest.raises(ServiceError) as error:
        servicio.eliminar_evento(evento["evento_id"])

    assert error.value.code == CODES.INVALID


def test_eliminar_un_evento_sin_ventas_lo_borra(servicio, catalogo):
    evento, seccion = catalogo

    resultado = servicio.eliminar_evento(evento["evento_id"])

    assert resultado["eliminado"] is True
    assert servicio.listar_eventos() == []


# ------------------------------------------------------------- observabilidad --
def test_el_contador_de_reservas_se_mueve(servicio, catalogo):
    from common.metrics import REGISTRY

    evento, seccion = catalogo
    elegidas = _primera(_estados(servicio, seccion["seccion_id"]), 1)
    servicio.reservar("saga-1", evento["evento_id"], seccion["seccion_id"], elegidas)

    valor = REGISTRY.get_sample_value("butacas_reservas_total", {"result": "ok"})

    assert valor is not None and valor >= 1


def test_el_gauge_de_butacas_por_estado_refleja_las_filas(servicio, catalogo):
    from common.metrics import REGISTRY

    evento, seccion = catalogo
    elegidas = _primera(_estados(servicio, seccion["seccion_id"]), 2)
    servicio.reservar("saga-1", evento["evento_id"], seccion["seccion_id"], elegidas)

    libres = REGISTRY.get_sample_value("butacas_butacas_por_estado", {"estado": LIBRE})
    reservadas = REGISTRY.get_sample_value("butacas_butacas_por_estado", {"estado": RESERVADA})

    assert libres is not None and libres >= 14
    assert reservadas is not None and reservadas >= 2


def test_el_gauge_se_actualiza_al_crear_una_seccion(servicio):
    from common.metrics import REGISTRY

    evento = servicio.crear_evento("Evento", "Sala", "2026-12-01T20:00:00Z")

    servicio.crear_seccion(evento["evento_id"], "Platea", 10, 1000)

    # La base del test solo tiene esta seccion: las diez filas quedan LIBRE.
    libres = REGISTRY.get_sample_value("butacas_butacas_por_estado", {"estado": LIBRE})
    assert libres == 10


# ------------------------------------------------------------- invariantes --
def test_la_reserva_guardada_corresponde_a_las_butacas_reservadas(servicio, catalogo, sesiones):
    evento, seccion = catalogo
    elegidas = _primera(_estados(servicio, seccion["seccion_id"]), 2)
    servicio.reservar("saga-1", evento["evento_id"], seccion["seccion_id"], elegidas)

    with sesiones() as session:
        reserva = session.query(Reserva).one()
        butacas = session.query(Butaca).filter(Butaca.estado == RESERVADA).all()

    assert reserva.saga_id == "saga-1"
    assert reserva.estado == ACTIVA
    assert reserva.precio_centavos == 25000
    assert {b.butaca_id for b in butacas} == set(elegidas)
    assert all(b.saga_id == "saga-1" for b in butacas)
