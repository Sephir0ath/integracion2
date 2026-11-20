"""Suite de pruebas del gateway.

Las pruebas corren **sin Redis y sin los servicios internos levantados**:
las tres dependencias externas (el cliente gRPC, el cliente de Redis y el
cliente HTTP del BFF) entran por los seams que deja el constructor de la
aplicacion, y se sustituyen por los dobles de `tests/fakes.py`.
"""
