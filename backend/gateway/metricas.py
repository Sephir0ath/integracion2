"""Metricas del gateway.

Nombres y etiquetas del contrato compartido (los leen los paneles de
Grafana y los scripts de evidencia):

* `gateway_requests_total{route,method,status}`;
* `gateway_request_duration_seconds{route}`;
* `gateway_rate_limit_rejected_total`;
* `gateway_auth_failures_total`.

La etiqueta `route` lleva la **plantilla** de la ruta (`/api/v1/compras/
{saga_id}`) y no la ruta concreta: asi el numero de series no crece con
las identificaciones de saga ni de butaca.
"""

from __future__ import annotations

from common.metrics import counter, histogram

solicitudes = counter(
    "gateway_requests_total",
    "Peticiones atendidas por el gateway, por ruta, metodo y estado",
    ["route", "method", "status"],
)
duracion = histogram(
    "gateway_request_duration_seconds",
    "Duracion de las peticiones atendidas por el gateway, por ruta",
    ["route"],
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0),
)
rechazos_limite = counter(
    "gateway_rate_limit_rejected_total",
    "Peticiones rechazadas por el limite de peticiones por cliente",
)
fallos_autenticacion = counter(
    "gateway_auth_failures_total",
    "Peticiones rechazadas porque la autenticacion no paso",
)

# Tabla de rutas del gateway: (plantilla, metodo, estado inicial). Las
# series se inicializan en cero para que el panel muestre el gateway desde
# el arranque, antes de que llegue la primera peticion.
RUTAS: tuple[tuple[str, str, str], ...] = (
    ("/api/v1/auth/token", "POST", "200"),
    ("/healthz", "GET", "200"),
    ("/metrics", "GET", "200"),
    ("/api/v1/butacas/eventos", "GET", "200"),
    ("/api/v1/butacas/secciones", "GET", "200"),
    ("/api/v1/butacas/secciones/{seccion_id}/butacas", "GET", "200"),
    ("/api/v1/butacas/eventos", "POST", "200"),
    ("/api/v1/butacas/eventos/{evento_id}", "PUT", "200"),
    ("/api/v1/butacas/eventos/{evento_id}", "DELETE", "200"),
    ("/api/v1/butacas/secciones", "POST", "200"),
    ("/api/v1/butacas/secciones/{seccion_id}", "PUT", "200"),
    ("/api/v1/butacas/secciones/{seccion_id}", "DELETE", "200"),
    ("/api/v1/compras", "POST", "201"),
    ("/api/v1/compras/{saga_id}", "GET", "200"),
    ("/api/v1/entradas", "GET", "200"),
    ("/bff/web/{ruta:path}", "GET", "200"),
    ("/bff/web/{ruta:path}", "POST", "200"),
)

for _ruta, _metodo, _estado in RUTAS:
    solicitudes.labels(_ruta, _metodo, _estado)
    duracion.labels(_ruta)
