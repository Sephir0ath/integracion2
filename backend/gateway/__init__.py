"""Gateway de la ticketera: el unico punto de entrada del sistema.

El gateway autentica, limita y enruta. Los servicios de dominio (butacas,
pagos, emision) y el orquestador de la saga (compras) **no se exponen**: el
gateway valida el token una sola vez, en la entrada, y reenvia la identidad
del cliente (`X-Client-Id` y `X-Role`) a los servicios internos, que
confian en ella y no vuelven a validar el token.

Ademas del enrutamiento, el gateway es el sitio donde viven dos de las
protecciones del sistema:

* la **autenticacion central** (`auth.py`): un JWT firmado con PyJWT que
  se emite en `POST /api/v1/auth/token`;
* el **limite de peticiones por cliente** (`rate_limit.py`): un tazon de
  fichas por cliente, con el estado en Redis y respaldo en memoria cuando
  Redis no responde.

El gateway es un servidor **HTTP** (FastAPI + uvicorn): habla gRPC con los
servicios de dominio y HTTP con el BFF.
"""
