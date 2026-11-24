#!/usr/bin/env python3
"""Escenario 2 · Fallo del ultimo paso y compensacion inversa.

Rompe `emision` (el **tercer** paso de la saga) y comprueba que los pasos ya
confirmados se compensan **en orden inverso** (REEMBOLSAR y luego LIBERAR),
sin dejar cobro sin operacion ni butaca bloqueada.

    python3 scripts/demo_compensacion.py
"""

from __future__ import annotations

import json
import subprocess
import sys

from _comun import (
    EMISION,
    RAIZ,
    Demo,
    api,
    api_bff,
    comprar,
    compensaciones_de,
    elegir_butacas,
    entradas_de,
    saga_de,
    fallar_fault,
    pasos_de,
    saga_id,
    token,
)


def logs_de_la_saga(saga: str) -> str:
    """Las lineas de log de la saga, tal como aparecen en el informe."""
    resultado = subprocess.run(
        ["docker", "compose", "logs", "compras", "--since=3m"],
        cwd=RAIZ, capture_output=True, text=True, check=False,
    )
    lineas = []
    for linea in resultado.stdout.splitlines():
        if saga not in linea:
            continue
        trozo = linea.split("|", 1)[-1].strip()
        try:
            evento = json.loads(trozo)
        except json.JSONDecodeError:
            lineas.append(trozo)
            continue
        partes = [evento.get("message", "")]
        for clave in ("paso", "detalle", "estado", "pasos"):
            if evento.get(clave):
                partes.append(f"{clave}={evento[clave]}")
        lineas.append(" ".join(partes))
    return "\n".join(lineas[-6:])


def main() -> int:
    demo = Demo("ESCENARIO 2 · FALLA EL ULTIMO PASO · compensacion en orden inverso")
    try:
        sesion = token()

        demo.seccion("1/6 · Se rompe la emision (el paso EMITIR de la saga)")
        estado = fallar_fault(EMISION, "down")
        demo.dato(f"emision:9003 → {estado}")

        evento_id, seccion_id, butacas = elegir_butacas(sesion, 1)
        saga = saga_id("demo-compensada")
        demo.dato(f"butaca {butacas[0]} de la seccion {seccion_id}")

        demo.seccion("2/6 · La compra falla en el ultimo paso")
        codigo, respuesta, segundos = comprar(sesion, saga, evento_id, seccion_id, butacas)
        demo.dato(f"POST /api/v1/compras → HTTP {codigo} en {segundos:.2f} s")
        datos = saga_de(respuesta)
        demo.dato(f"estado: {datos.get('estado')} · pasos: {pasos_de(datos)}")

        demo.ok(codigo == 201, "el gateway responde 201 Created")
        demo.ok(datos.get("estado") == "COMPENSADA", "la saga queda COMPENSADA")

        pasos = {p.get("nombre"): p.get("resultado") for p in datos.get("pasos", [])}
        demo.ok(pasos.get("RESERVAR") == "OK", "RESERVAR quedo OK (se compensara)")
        demo.ok(pasos.get("COBRAR") == "OK", "COBRAR quedo OK (se compensara)")
        demo.ok(pasos.get("EMITIR") == "ERROR", "EMITIR quedo ERROR (el paso que fallo)")

        demo.seccion("3/6 · Las compensaciones, en orden inverso")
        compensaciones = compensaciones_de(datos)
        demo.dato(f"compensaciones: {' → '.join(compensaciones) if compensaciones else '(ninguna)'}")
        demo.ok(compensaciones[:2] == ["REEMBOLSAR", "LIBERAR"],
                "se compensa REEMBOLSAR y despues LIBERAR (orden inverso)")

        demo.seccion("4/6 · No queda estado inconsistente")
        _, butacas_respuesta, _ = api_bff("GET", f"/bff/web/secciones/{seccion_id}/butacas", None, sesion)
        estados = {b["butaca_id"]: b.get("estado") for b in butacas_respuesta.get("butacas", [])}
        demo.dato(f"estado de {butacas[0]}: {estados.get(butacas[0])}")
        demo.ok(estados.get(butacas[0]) == "LIBRE",
                "la butaca vuelve a LIBRE (no queda reservada sin venta)")

        propias = entradas_de(sesion, saga)
        demo.ok(len(propias) == 0, "no se emitio ninguna entrada (no hay cobro sin operacion)")

        demo.seccion("5/6 · La evidencia: los logs de la saga")
        for linea in logs_de_la_saga(saga).splitlines():
            demo.dato(linea.split("compras-1  | ")[-1][:150])

        demo.seccion("6/6 · Se restaura la emision")
        fallar_fault(EMISION, "ok")
        demo.dato("emision:9003 → ok")
    finally:
        # Aunque el script reviente, la emision tiene que quedar sana.
        try:
            fallar_fault(EMISION, "ok")
        except Exception:  # noqa: BLE001 - la restauracion es lo ultimo que se intenta
            pass

    return demo.cierre()


if __name__ == "__main__":
    sys.exit(main())
