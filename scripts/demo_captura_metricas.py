#!/usr/bin/env python3
"""Escenario 4 · Captura de la evidencia de Grafana.

Provoca la falla en cascada, espera a que Prometheus recoja las metricas y
guarda las capturas de los dashboards de Grafana (Figuras 3 y 4 del informe)
junto con los valores crudos de las metricas.

    python3 scripts/demo_captura_metricas.py

Necesita el perfil de observabilidad levantado:

    docker compose --profile observability up -d
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

from _comun import (
    GRAFANA,
    PROMETHEUS,
    RAIZ,
    Demo,
    metrica,
    token,
)

DESTINO = RAIZ / "docs" / "evidencia"

# Las consultas que aparecen en los dashboards, para volcar los valores crudos.
CONSULTAS = {
    "circuit_breaker_state": 'circuit_breaker_state{breaker="pasarela"}',
    "circuit_breaker_transitions_total": 'circuit_breaker_transitions_total{breaker="pasarela"}',
    "circuit_breaker_rejected_total": 'circuit_breaker_rejected_total{breaker="pasarela"}',
    "bulkhead_in_use": 'bulkhead_in_use{bulkhead="pasarela"}',
    "bulkhead_rejected_total": 'bulkhead_rejected_total{bulkhead="pasarela"}',
}

PANELES = {
    "ticketera-circuit-breaker": {
        "completo": "figura-3-circuit-breaker.png",
        "panel": "figura-3-estado-del-circuito.png",
        "panel_id": 2,
    },
    "ticketera-bulkhead": {
        "completo": "figura-4-bulkhead.png",
        "panel": "figura-4-bulkhead-rechazos.png",
        "panel_id": 4,
    },
}


def capturar(dashboard: str, destino: Path, panel_id: int | None = None) -> bool:
    """Captura un panel (o el dashboard entero) con Chromium headless."""
    url = f"{GRAFANA}/d/{dashboard}/x?orgId=1&from=now-30m&to=now&kiosk"
    if panel_id:
        url += f"&viewPanel={panel_id}"
    comando = [
        "chromium", "--headless", "--no-sandbox", "--disable-gpu",
        "--disable-dev-shm-usage", "--hide-scrollbars",
        "--virtual-time-budget=10000", "--window-size=1500,700",
        f"--screenshot={destino}", url,
    ]
    try:
        subprocess.run(comando, timeout=90, check=True, capture_output=True)
    except (subprocess.TimeoutExpired, subprocess.CalledProcessError) as error:
        print(f"      no se pudo capturar {destino.name}: {error}")
        return False
    return destino.exists()


def esperar_prometheus(objetivos: int = 7) -> bool:
    """Espera a que Prometheus tenga todos los objetivos arriba."""
    for _ in range(30):
        try:
            with urllib.request.urlopen(f"{PROMETHEUS}/api/v1/targets?state=active", timeout=5) as r:
                datos = json.loads(r.read().decode())
        except Exception:  # noqa: BLE001 - todavia no esta listo
            time.sleep(2)
            continue
        activos = datos.get("data", {}).get("activeTargets", [])
        arriba = [t for t in activos if t.get("health") == "up"]
        if len(arriba) >= objetivos:
            return True
        time.sleep(2)
    return False


def volcar_metricas(destino: Path) -> None:
    """Vuelca los valores de las metricas que aparecen en los dashboards."""
    lineas = ["# Valores de las metricas en el momento de la captura",
              f"# generadas el {time.strftime('%Y-%m-%d %H:%M:%S')}", ""]
    for nombre, consulta in CONSULTAS.items():
        series = metrica(consulta)
        lineas.append(f"## {nombre}")
        lineas.append(f"consulta: {consulta}")
        if not series:
            lineas.append("  (sin series: la metrica todavia no existe)")
        for serie in series:
            etiquetas = {k: v for k, v in serie["metric"].items() if k != "__name__"}
            valor = serie["value"][1]
            lineas.append(f"  {etiquetas} = {valor}")
        lineas.append("")
    destino.write_text("\n".join(lineas) + "\n")


def main() -> int:
    demo = Demo("ESCENARIO 4 · CAPTURA DE LA EVIDENCIA EN GRAFANA")
    try:
        demo.seccion("1/4 · El perfil de observabilidad")
        subprocess.run(["docker", "compose", "--profile", "observability", "up", "-d"],
                       cwd=RAIZ, check=False, capture_output=True)
        listo = esperar_prometheus()
        demo.ok(listo, "Prometheus tiene sus 7 objetivos arriba")
        if not listo:
            return demo.cierre()

        demo.seccion("2/4 · Se provoca la falla en cascada")
        from demo_dependencia_fragil import (  # noqa: PLC0415 - se importa cuando ya hace falta
            fase_bulkhead,
            fase_circuito_abre,
            fase_recuperacion,
        )

        sesion = token()
        fase_circuito_abre(sesion, demo)
        fase_bulkhead(sesion, demo)

        demo.seccion("3/4 · Se espera a que Prometheus recoja las metricas")
        # El scrape es cada 15 s: dos ciclos ensures que los datos esten.
        for segundos in (16, 16):
            print(f"      esperando {segundos} s...", flush=True)
            time.sleep(segundos)
        demo.dato("el circuito recorrio el ciclo y los rechazos quedaron acumulados")

        demo.seccion("4/4 · Capturas")
        DESTINO.mkdir(parents=True, exist_ok=True)
        for dashboard, archivos in PANELES.items():
            completo = DESTINO / archivos["completo"]
            panel = DESTINO / archivos["panel"]
            ok_completo = capturar(dashboard, completo)
            ok_panel = capturar(dashboard, panel, archivos["panel_id"])
            demo.dato(f"{dashboard}: {completo.name} {'OK' if ok_completo else 'FALLO'}"
                      f" · {panel.name} {'OK' if ok_panel else 'FALLO'}")
            demo.ok(ok_completo and ok_panel, f"capturas de {dashboard}")

        volcar_metricas(DESTINO / "metricas.txt")
        demo.dato(f"valores crudos: {(DESTINO / 'metricas.txt').name}")

        demo.seccion("Se restaura la pasarela")
        from _comun import PASARELA, fallar_fault  # noqa: PLC0415

        fallar_fault(PASARELA, "ok")
        fase_recuperacion(sesion, demo)
    finally:
        try:
            from _comun import PASARELA, fallar_fault  # noqa: PLC0415

            fallar_fault(PASARELA, "ok")
        except Exception:  # noqa: BLE001
            pass

    return demo.cierre()


if __name__ == "__main__":
    sys.exit(main())
