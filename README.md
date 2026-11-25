# Ticketera

Sistema distribuido de venta de entradas: tres servicios de dominio con base
de datos propia, un gateway como única puerta de entrada, un BFF que agrega
los datos y sirve la interfaz web, y la compra orquestada como Saga con
compensaciones.

**Decisiones:** Saga por orquestación (el orquestador fija el orden y compensa
en reversa), gateway y BFF como único borde publicado, Redis como estado
compartido del límite de peticiones.

## Levantar el sistema

    docker compose up -d --build
    docker compose --profile observability up -d

| Qué | Dónde |
|---|---|
| Interfaz web | http://localhost:8081 |
| API (gateway) | http://localhost:8080 |
| Prometheus / Grafana | `docker compose --profile observability up -d` → :9090 / :3000 |

Usuarios de demostración: `cliente@ticketera.local` / `cliente123` (comprar) y
`admin@ticketera.local` / `admin123` (administración del recinto).

## Pruebas

    # suites de los servicios, dentro de los contenedores ya levantados
    for s in butacas pagos emision compras gateway bff; do
      docker compose exec -T $s python -m pytest /app/service/tests -q
    done

    # end-to-end con navegador real
    cd tests/e2e && npm install && npx playwright test

## Provocar una falla

La falla se inyecta por `POST /admin/fault` en `emision` y `pasarela` (modos
`ok`, `slow`, `down`). Sus puertos son internos, así que se llaman desde
dentro de la red de compose.

```bash
python scripts/demo_camino_feliz.py
python scripts/demo_compensacion.py
python scripts/demo_dependencia_fragil.py
```
1. **Camino feliz**: comprar entradas; la saga queda `CONFIRMADA` con sus tres
   pasos (reserva, cobro, emisión).
2. **Fallo con compensación**: dejar `emision` en `down` y comprar; el pago se
   revierte y la butaca se liberan, en orden inverso.
3. **Falla en cascada contenida**: dejar `pasarela` en `slow`; el circuito abre
   y responde rápido (fail-fast), el bulkhead rechaza el exceso y el resto del
   sistema sigue respondiendo. Evidencia: Prometheus (:9090) y Grafana (:3000).

## Estructura

    backend/common/                      biblioteca compartida (errores, métricas, resiliencia)
    backend/butacas/ pagos/ emision/     dominios, cada uno con su base de datos
    backend/compras/                     orquestador de la saga
    backend/gateway/                     puerta de entrada (auth, rate limit, ruteo)
    backend/bff/                         agregación + interfaz web
    frontend/web/                        interfaz
    proto/                               contratos gRPC
    tests/e2e/                           pruebas end-to-end con Playwright
    docs/                                evidencia
    observability/                       prometheus y grafana
