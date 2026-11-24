#!/usr/bin/env python3
"""Escenario 3 · Falla en cascada contenida (pasarela lenta y luego caida).

Demuestra que el **circuit breaker** abre y responde rapido (fail-fast), que
el **bulkhead** rechaza el exceso sin que nadie espere, y que el resto del
sistema sigue funcionando mientras la dependencia fragil esta mala.

    python3 scripts/demo_dependencia_fragil.py

Las fases se pueden usar sueltas desde `demo_captura_metricas.py`.
"""

from __future__ import annotations

import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from _comun import (
    PASARELA,
    Demo,
    api,
    api_bff,
    comprar,
    elegir_butacas,
    fallar_fault,
    pasos_de,
    saga_de,
    saga_id,
    seccion_con_mas_libres,
    token,
    valor_metrica,
)


def estado_del_circuito() -> str:
    """El estado del circuito leido de Prometheus."""
    series = []
    from _comun import metrica  # import local: solo se usa en el relato

    for serie in metrica('circuit_breaker_state{breaker="pasarela"} == 1'):
        series.append(f"{serie['metric'].get('state')}={serie['value'][1]}")
    return " ".join(series) or "sin datos"


def comprar_una(sesion, prefijo: str) -> tuple[int, dict, float]:
    """Una compra cualquiera (con su propia saga y su butaca)."""
    evento_id, seccion_id, butacas = elegir_butacas(sesion, 1)
    saga = saga_id(prefijo)
    return comprar(sesion, saga, evento_id, seccion_id, butacas)


def fase_circuito_abre(sesion, demo: Demo) -> dict:
    """Fase A: con la pasarela lenta, el circuito abre y el fail-fast se nota."""
    demo.seccion("FASE A · la pasarela se pone lenta (3 s, mas que el timeout de 2 s)")
    fallar_fault(PASARELA, "slow", delay_ms=3000)
    demo.dato("pasarela:9500 → slow (delay_ms=3000)")

    tiempos = []
    estados = []
    for numero in range(1, 6):
        codigo, respuesta, segundos = comprar_una(sesion, "demo-lenta")
        datos = saga_de(respuesta)
        tiempos.append(segundos)
        estados.append(datos.get("estado"))
        demo.dato(
            f"compra {numero}: HTTP {codigo} en {segundos:5.2f} s · "
            f"{datos.get('estado')} · {pasos_de(datos)[:44]}"
        )

    demo.ok(estados[-1] in ("COMPENSADA", "FALLIDA"),
            "las compras que pasan por la pasarela lenta se compensan (no queda nada a medias)")
    demo.ok(tiempos[-1] < tiempos[0] / 2,
            f"el fail-fast se nota: la ultima compra tardo {tiempos[-1]:.2f} s "
            f"contra {tiempos[0]:.2f} s de la primera")
    demo.dato(f"circuito: {estado_del_circuito()}")
    return {"tiempos": tiempos, "estados": estados}


def cerrar_circuito(sesion, demo: Demo) -> bool:
    """Devuelve el circuito a CLOSED.

    El circuito no se cierra solo: cuando expira el reset (8 s) queda en
    HALF_OPEN y espera **una llamada** para probar la dependencia. Si nadie
    llama, sigue en OPEN. Por eso hay que hacer una llamada de sondeo.
    """
    fallar_fault(PASARELA, "ok")
    time.sleep(10)  # el reset timeout del circuito es de 8 s
    codigo, respuesta, _ = comprar_una(sesion, "demo-sondeo")
    demo.dato(f"llamada de sondeo: HTTP {codigo} · {saga_de(respuesta).get('estado')}")
    time.sleep(16)  # un scrape de Prometheus, para que el gauge lo refleje
    return "CLOSED=1" in estado_del_circuito()


def fase_bulkhead(sesion, demo: Demo) -> dict:
    """Fase B: con el circuito cerrado, una rafaga satura el bulkhead."""
    demo.seccion("FASE B · el circuito se cierra y la rafaga satura el bulkhead")
    cerrado = cerrar_circuito(sesion, demo)
    demo.dato(f"circuito: {estado_del_circuito()} (cerrado: las llamadas vuelven a la pasarela)")
    demo.ok(cerrado, "el circuito se cierra solo tras el reset (CLOSED -> HALF_OPEN -> CLOSED)")
    fallar_fault(PASARELA, "slow", delay_ms=3000)

    # Las butacas se eligen UNA vez: si cada compra busca la suya, las
    # llamadas se desfasan y el pico nunca llega a saturar el bulkhead.
    evento_id, seccion_id, butacas = seccion_con_mas_libres(sesion)
    total = min(22, len(butacas))

    def golpe(indice: int) -> tuple[int, dict, float]:
        try:
            saga = saga_id(f"demo-pico-{indice}")
            return comprar(sesion, saga, evento_id, seccion_id, [butacas[indice]])
        except Exception as error:  # noqa: BLE001 - un rechazo del gateway no es fatal
            return 0, {"compra": {"estado": f"error: {error}"}}, 0.0

    # Mientras dura el pico se sigue consultando el catalogo: es la prueba
    # de que el resto del sistema no se cae mientras la dependencia falla.
    sondeo = {"codigos": []}
    seguir = threading.Event()

    def preguntar_catalogo() -> None:
        while not seguir.is_set():
            codigo, _, _ = api_bff("GET", "/bff/web/catalogo", None, sesion)
            sondeo["codigos"].append(codigo)
            time.sleep(0.5)

    hilo = threading.Thread(target=preguntar_catalogo, daemon=True)
    hilo.start()
    inicio = time.perf_counter()
    with ThreadPoolExecutor(max_workers=total) as grupo:
        resultados = list(grupo.map(golpe, range(total)))
    duracion = time.perf_counter() - inicio
    seguir.set()
    hilo.join(timeout=3)

    limitados = 0
    saturadas = 0
    confirmadas = 0
    for codigo, respuesta, segundos in resultados:
        if codigo == 429:
            limitados += 1
            continue
        datos = saga_de(respuesta)
        estado = datos.get("estado", "")
        if "saturada" in str(datos.get("error", "")):
            saturadas += 1
        if estado == "CONFIRMADA":
            confirmadas += 1

    demo.dato(f"{total} compras simultaneas en {duracion:.1f} s (seccion {seccion_id})")
    demo.dato(f"  {limitados} rechazadas por el rate limit del gateway (HTTP 429)")
    demo.dato(f"  {saturadas} con la pasarela saturada (RESOURCE_EXHAUSTED, rechazo inmediato)")
    demo.dato(f"  {confirmadas} confirmadas")

    # Lo importante: mientras la dependencia esta mala, el catalogo responde.
    codigos = sondeo["codigos"]
    ok_200 = sum(1 for c in codigos if c == 200)
    demo.dato(f"catálogo consultado durante el pico: {len(codigos)} veces, "
              f"{ok_200} con HTTP 200")
    demo.ok(len(codigos) > 0 and ok_200 == len(codigos),
            "el resto del sistema sigue respondiendo con normalidad")
    demo.ok(saturadas > 0, "el bulkhead rechazo el exceso (nadie espera de mas)")
    return {"saturadas": saturadas, "limitados": limitados}


def fase_recuperacion(sesion, demo: Demo) -> dict:
    """Fase C: al restaurar la dependencia, todo se recupera solo."""
    demo.seccion("FASE C · se restaura la pasarela y el sistema se recupera")
    fallar_fault(PASARELA, "ok")
    demo.dato("pasarela:9500 → ok")
    time.sleep(10)  # el reset timeout del circuito
    codigo, respuesta, segundos = comprar_una(sesion, "demo-recuperada")
    datos = saga_de(respuesta)
    demo.dato(f"compra → HTTP {codigo} en {segundos:.2f} s · {datos.get('estado')}")
    time.sleep(16)  # un scrape para que el gauge refleje el cierre
    demo.dato(f"circuito: {estado_del_circuito()}")
    demo.ok(datos.get("estado") == "CONFIRMADA", "la compra vuelve a CONFIRMADA")
    demo.ok(segundos < 2.0, "y responde rapido (sin la latencia de la dependencia)")
    return {"segundos": segundos}


def main() -> int:
    demo = Demo("ESCENARIO 3 · FALLA EN CASCADA CONTENIDA · circuito, bulkhead y recuperacion")
    try:
        sesion = token()
        fase_circuito_abre(sesion, demo)
        fase_bulkhead(sesion, demo)
        fase_recuperacion(sesion, demo)
        demo.seccion("METRICAS")
        demo.dato(f"circuit_breaker_rejected_total = {valor_metrica('circuit_breaker_rejected_total{breaker=\"pasarela\"}')}")
        demo.dato(f"bulkhead_rejected_total = {valor_metrica('bulkhead_rejected_total{bulkhead=\"pasarela\"}')}")
    finally:
        try:
            fallar_fault(PASARELA, "ok")
        except Exception:  # noqa: BLE001 - la restauracion es lo ultimo que se intenta
            pass

    return demo.cierre()


if __name__ == "__main__":
    sys.exit(main())
