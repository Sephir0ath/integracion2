"""Pruebas del enrutamiento del gateway.

El gateway no tiene logica de negocio: cada ruta proxied devuelve el JSON
con la forma exacta del contrato, con los mismos nombres de campo que los
protos (snake_case).
"""

from __future__ import annotations

# Forma exacta de cada objeto del contrato.
FORMAS = {
    "evento": {"evento_id", "nombre", "lugar", "fecha"},
    "seccion": {
        "seccion_id",
        "evento_id",
        "nombre",
        "capacidad_total",
        "cantidad_disponible",
        "precio_centavos",
    },
    "butaca": {"butaca_id", "seccion_id", "fila", "numero", "estado", "saga_id"},
    "compra": {
        "saga_id",
        "estado",
        "pasos",
        "compensaciones",
        "evento_id",
        "seccion_id",
        "cantidad",
        "cliente_id",
        "error",
        "actualizada_en",
        "monto_total_centavos",
        "moneda",
    },
    "entrada": {"entrada_id", "saga_id", "butaca_id", "fila", "numero", "qr", "estado"},
}


async def test_listar_eventos_devuelve_la_forma_del_contrato(http, tokens):
    """`GET /api/v1/butacas/eventos` -> `{"eventos":[...]}`."""
    respuesta = await http.get(
        "/api/v1/butacas/eventos",
        headers={"Authorization": f"Bearer {tokens.cliente()}"},
    )

    assert respuesta.status_code == 200
    cuerpo = respuesta.json()
    assert set(cuerpo) == {"eventos"}
    assert len(cuerpo["eventos"]) == 2
    assert set(cuerpo["eventos"][0]) == FORMAS["evento"]


async def test_listar_secciones_devuelve_la_forma_del_contrato(http, tokens):
    """`GET /api/v1/butacas/secciones` -> `{"secciones":[...]}`."""
    respuesta = await http.get(
        "/api/v1/butacas/secciones",
        params={"evento_id": "ev-1"},
        headers={"Authorization": f"Bearer {tokens.cliente()}"},
    )

    assert respuesta.status_code == 200
    cuerpo = respuesta.json()
    assert set(cuerpo) == {"secciones"}
    assert set(cuerpo["secciones"][0]) == FORMAS["seccion"]
    assert cuerpo["secciones"][0]["precio_centavos"] == 55000


async def test_plano_de_la_sala_devuelve_la_forma_del_contrato(http, tokens):
    """`GET /api/v1/butacas/secciones/{id}/butacas` -> `{"butacas":[...]}`."""
    respuesta = await http.get(
        "/api/v1/butacas/secciones/sec-1/butacas",
        headers={"Authorization": f"Bearer {tokens.cliente()}"},
    )

    assert respuesta.status_code == 200
    cuerpo = respuesta.json()
    assert set(cuerpo) == {"butacas"}
    assert set(cuerpo["butacas"][0]) == FORMAS["butaca"]
    assert cuerpo["butacas"][0]["estado"] == "LIBRE"


async def test_crear_evento_devuelve_la_forma_del_contrato(http, tokens):
    """`POST /api/v1/butacas/eventos` -> `{"evento":{...}}`."""
    respuesta = await http.post(
        "/api/v1/butacas/eventos",
        json={"nombre": "Recital", "lugar": "Teatro Colon", "fecha": "2026-05-01T20:00:00"},
        headers={"Authorization": f"Bearer {tokens.admin()}"},
    )

    assert respuesta.status_code == 200
    cuerpo = respuesta.json()
    assert set(cuerpo) == {"evento"}
    assert set(cuerpo["evento"]) == FORMAS["evento"]


async def test_actualizar_evento_devuelve_la_forma_del_contrato(http, tokens):
    """`PUT /api/v1/butacas/eventos/{id}` -> `{"evento":{...}}`."""
    respuesta = await http.put(
        "/api/v1/butacas/eventos/ev-1",
        json={"nombre": "Recital", "lugar": "Teatro Colon", "fecha": "2026-05-01T20:00:00"},
        headers={"Authorization": f"Bearer {tokens.admin()}"},
    )

    assert respuesta.status_code == 200
    assert set(respuesta.json()["evento"]) == FORMAS["evento"]


async def test_eliminar_evento_devuelve_eliminado(http, tokens):
    """`DELETE /api/v1/butacas/eventos/{id}` -> `{"eliminado":true}`."""
    respuesta = await http.delete(
        "/api/v1/butacas/eventos/ev-1",
        headers={"Authorization": f"Bearer {tokens.admin()}"},
    )

    assert respuesta.status_code == 200
    assert respuesta.json() == {"eliminado": True}


async def test_crear_seccion_devuelve_la_forma_del_contrato(http, tokens):
    """`POST /api/v1/butacas/secciones` -> `{"seccion":{...}}`."""
    respuesta = await http.post(
        "/api/v1/butacas/secciones",
        json={
            "evento_id": "ev-1",
            "nombre": "Grada",
            "capacidad_total": 10,
            "precio_centavos": 1000,
        },
        headers={"Authorization": f"Bearer {tokens.admin()}"},
    )

    assert respuesta.status_code == 200
    assert set(respuesta.json()["seccion"]) == FORMAS["seccion"]


async def test_actualizar_seccion_devuelve_la_forma_del_contrato(http, tokens):
    """`PUT /api/v1/butacas/secciones/{id}` -> `{"seccion":{...}}`."""
    respuesta = await http.put(
        "/api/v1/butacas/secciones/sec-1",
        json={"nombre": "Grada", "capacidad_total": 10, "precio_centavos": 1000},
        headers={"Authorization": f"Bearer {tokens.admin()}"},
    )

    assert respuesta.status_code == 200
    assert set(respuesta.json()["seccion"]) == FORMAS["seccion"]


async def test_eliminar_seccion_devuelve_eliminado(http, tokens):
    """`DELETE /api/v1/butacas/secciones/{id}` -> `{"eliminado":true}`."""
    respuesta = await http.delete(
        "/api/v1/butacas/secciones/sec-1",
        headers={"Authorization": f"Bearer {tokens.admin()}"},
    )

    assert respuesta.status_code == 200
    assert respuesta.json() == {"eliminado": True}


async def test_entradas_devuelve_la_forma_del_contrato(http, tokens):
    """`GET /api/v1/entradas` -> `{"entradas":[...]}`."""
    respuesta = await http.get(
        "/api/v1/entradas",
        params={"saga_id": "saga-1"},
        headers={"Authorization": f"Bearer {tokens.cliente()}"},
    )

    assert respuesta.status_code == 200
    cuerpo = respuesta.json()
    assert set(cuerpo) == {"entradas"}
    assert len(cuerpo["entradas"]) == 2
    assert set(cuerpo["entradas"][0]) == FORMAS["entrada"]
    assert cuerpo["entradas"][0]["qr"].startswith("TKT-")


# ------------------------------------------------------------- la saga --


async def test_consultar_compra_devuelve_la_forma_del_contrato(http, tokens):
    """`GET /api/v1/compras/{saga_id}` -> `{"compra":{...}}`."""
    respuesta = await http.get(
        "/api/v1/compras/saga-1",
        headers={"Authorization": f"Bearer {tokens.cliente()}"},
    )

    assert respuesta.status_code == 200
    cuerpo = respuesta.json()
    assert set(cuerpo) == {"compra"}
    assert set(cuerpo["compra"]) == FORMAS["compra"]
    assert cuerpo["compra"]["estado"] == "CONFIRMADA"
    assert [paso["nombre"] for paso in cuerpo["compra"]["pasos"]] == ["RESERVAR"]


async def test_iniciar_compra_responde_201_con_la_compra(http, tokens, stubs):
    """`POST /api/v1/compras` -> `201 {"compra":{...}}` con el flag idempotente."""
    respuesta = await http.post(
        "/api/v1/compras",
        json={
            "saga_id": "saga-1",
            "evento_id": "ev-1",
            "seccion_id": "sec-1",
            "butaca_ids": ["sec-1-A1", "sec-1-A2"],
            "cliente_id": "otro-cliente",
        },
        headers={"Authorization": f"Bearer {tokens.cliente()}"},
    )

    assert respuesta.status_code == 201
    cuerpo = respuesta.json()
    assert set(cuerpo) == {"compra", "idempotente"}
    assert set(cuerpo["compra"]) == FORMAS["compra"]
    assert cuerpo["compra"]["saga_id"] == "saga-1"
    assert cuerpo["compra"]["estado"] == "CONFIRMADA"
    assert cuerpo["compra"]["cantidad"] == 2
    assert cuerpo["compra"]["monto_total_centavos"] == 110000
    assert cuerpo["idempotente"] is False
    # El cliente lo pone el token, nunca el cuerpo de la peticion.
    peticion = stubs["compras"].ultima_llamada["request"]
    assert peticion.cliente_id == "cliente-1"
    assert peticion.butaca_ids == ["sec-1-A1", "sec-1-A2"]


async def test_iniciar_compra_idempotente_por_saga_id(http, tokens, stubs):
    """La misma saga repetida devuelve la respuesta idempotente."""
    stubs["compras"].idempotente = True

    respuesta = await http.post(
        "/api/v1/compras",
        json={"saga_id": "saga-1", "evento_id": "ev-1", "seccion_id": "sec-1", "butaca_ids": ["sec-1-A1"]},
        headers={"Authorization": f"Bearer {tokens.cliente()}"},
    )

    assert respuesta.status_code == 201
    assert respuesta.json()["idempotente"] is True


async def test_entradas_sin_saga_id_es_400(http, tokens):
    """Sin `saga_id` la peticion no se puede resolver."""
    respuesta = await http.get(
        "/api/v1/entradas",
        headers={"Authorization": f"Bearer {tokens.cliente()}"},
    )

    assert respuesta.status_code == 400


async def test_iniciar_compra_compensada_devuelve_las_compensaciones(http, tokens, stubs):
    """Una saga compensada se devuelve con sus compensaciones, no con un 500.

    El borde arma la respuesta con los datos del contrato: cuando la saga
    compensó, la respuesta incluye las compensaciones aplicadas. Si al
    traducirlas el borde leyera un atributo que no existe, la respuesta
    seria un 500 opaco y el cliente perderia el estado de la saga.
    """
    stubs["compras"].compensada = True

    respuesta = await http.post(
        "/api/v1/compras",
        json={"saga_id": "saga-1", "evento_id": "ev-1", "seccion_id": "sec-1", "butaca_ids": ["sec-1-A1"]},
        headers={"Authorization": f"Bearer {tokens.cliente()}"},
    )

    assert respuesta.status_code == 201
    cuerpo = respuesta.json()
    assert cuerpo["compra"]["estado"] == "COMPENSADA"
    assert [c["nombre"] for c in cuerpo["compra"]["compensaciones"]] == ["REEMBOLSAR", "LIBERAR"]
    assert all("finalizado_en" in c for c in cuerpo["compra"]["compensaciones"])
