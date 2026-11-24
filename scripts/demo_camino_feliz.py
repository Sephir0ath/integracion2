#!/usr/bin/env python3
"""Escenario 1 · Camino feliz.

Demuestra que la compra se completa: los tres pasos de la saga quedan
firmes, la butaca pasa a `VENDIDA` y se emite la entrada con su QR.

    python3 scripts/demo_camino_feliz.py
"""

from __future__ import annotations

import sys

from _comun import (
    Demo,
    api,
    api_bff,
    butacas_libres,
    comprar,
    elegir_butacas,
    entradas_de,
    pasos_de,
    saga_de,
    saga_id,
    token,
)


def main() -> int:
    demo = Demo("ESCENARIO 1 · CAMINO FELIZ · la saga llega a CONFIRMADA")
    try:
        demo.seccion("1/5 · Token del cliente")
        sesion = token()
        demo.dato("cliente@ticketera.local → cliente-1")

        demo.seccion("2/5 · Se elige una butaca libre del catalogo")
        evento_id, seccion_id, butacas = elegir_butacas(sesion, 1)
        demo.dato(f"evento {evento_id} · seccion {seccion_id} · butaca {butacas[0]}")

        saga = saga_id("demo-feliz")
        demo.seccion("3/5 · La compra")
        codigo, respuesta, segundos = comprar(sesion, saga, evento_id, seccion_id, butacas)
        demo.dato(f"POST /api/v1/compras → HTTP {codigo} en {segundos:.2f} s")
        datos = saga_de(respuesta)
        demo.dato(f"estado: {datos.get('estado')} · pasos: {pasos_de(datos)}")

        demo.ok(codigo == 201, "el gateway responde 201 Created")
        demo.ok(datos.get("estado") == "CONFIRMADA", "la saga queda CONFIRMADA")
        demo.ok(
            [p.get("resultado") for p in datos.get("pasos", [])] == ["OK", "OK", "OK"],
            "los tres pasos (RESERVAR, COBRAR, EMITIR) quedan OK",
        )

        demo.seccion("4/5 · La butaca queda VENDIDA y la entrada existe")
        _, butacas_respuesta, _ = api_bff("GET", f"/bff/web/secciones/{seccion_id}/butacas", None, sesion)
        estados = {b["butaca_id"]: b.get("estado") for b in butacas_respuesta.get("butacas", [])}
        demo.dato(f"estado de {butacas[0]}: {estados.get(butacas[0])}")
        demo.ok(estados.get(butacas[0]) == "VENDIDA", "la butaca pasa a VENDIDA")

        propias = entradas_de(sesion, saga)
        demo.dato(f"entradas de esta saga: {len(propias)}")
        demo.ok(len(propias) >= 1, "se emitio la entrada con su QR")

        demo.seccion("5/5 · La saga es idempotente")
        codigo, repetida, _ = api("GET", f"/api/v1/compras/{saga}", None, sesion)
        demo.ok(codigo == 200 and saga_de(repetida).get("estado") == "CONFIRMADA",
                "consultar la saga de nuevo devuelve el mismo estado")
    finally:
        pass

    return demo.cierre()


if __name__ == "__main__":
    sys.exit(main())
