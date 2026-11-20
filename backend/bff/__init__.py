"""BFF de la ticketera: la capa a medida del cliente.

El BFF (backend for frontend) hace dos cosas que un proxy no hace:

1. **Compone** en una sola respuesta los datos de dos o mas servicios: el
   catalogo junta `butacas` y `pagos`, la compra junta `compras` y
   `emision`, y la consulta de una saga junta `compras`, `butacas` y
   `emision`.
2. **Degrada con elegancia**: cuando una dependencia *opcional* falla, la
   respuesta sigue siendo 200 con lo que hay y marcada como degradada; una
   dependencia *esencial* que falla se traduce a 503 con el codigo de
   `common.errors`, nunca a un 500 opaco.

Ademas el BFF es el unico contenedor que lleva la interfaz web (el encargo
exige que solo el gateway y el BFF queden expuestos al exterior), asi que
sirve `frontend/web/` como archivos estaticos en `/`.

El BFF es un servidor **HTTP** (FastAPI + uvicorn) que habla gRPC con los
servicios internos. El puerto sale de `BFF_PORT` (8081 por defecto) y el
healthcheck del compose pide `GET /healthz`.

El BFF **no revalida el JWT**: el gateway ya lo valido y reenvia la
identidad del cliente en `X-Client-Id` y `X-Role`. Las rutas de negocio
exigen `X-Client-Id`: su ausencia significa que la peticion no vino por la
puerta de entrada.
"""
