"""Metricas del BFF.

Nombres y etiquetas del contrato compartido (los leen los paneles de
Grafana y los scripts de evidencia):

* `bff_requests_total{route,method,status}`;
* `bff_request_duration_seconds{route}`.

La etiqueta `route` lleva la **plantilla** de la ruta
(`/bff/web/compras/{saga_id}`) y no la ruta concreta: asi el numero de
series no crece con cada identificacion de saga ni de butaca.
"""

from __future__ import annotations

from common.metrics import counter, histogram

solicitudes = counter(
    "bff_requests_total",
    "Peticiones atendidas por el BFF, por ruta, metodo y estado",
    ["route", "method", "status"],
)
duracion = histogram(
    "bff_request_duration_seconds",
    "Duracion de las peticiones atendidas por el BFF, por ruta",
    ["route"],
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0),
)

# Tabla de rutas del BFF: (plantilla, metodo, estado inicial). Las series se
# inicializan en cero para que el panel muestre el BFF desde el arranque,
# antes de que llegue la primera peticion.
RUTAS: tuple[tuple[str, str, str], ...] = (
    ("/healthz", "GET", "200"),
    ("/metrics", "GET", "200"),
    ("/bff/web/catalogo", "GET", "200"),
    ("/bff/web/secciones/{seccion_id}/butacas", "GET", "200"),
    ("/bff/web/compras", "POST", "200"),
    ("/bff/web/compras/{saga_id}", "GET", "200"),
)

for _ruta, _metodo, _estado in RUTAS:
    solicitudes.labels(_ruta, _metodo, _estado)
    duracion.labels(_ruta)
