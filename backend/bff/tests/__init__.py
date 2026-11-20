"""Suite de pruebas del BFF.

Las pruebas corren **sin los servicios internos levantados**: los cuatro
clientes gRPC (butacas, pagos, emision y compras) entran por los seams que
deja el constructor de la aplicacion y se sustituyen por los dobles de
`tests/fakes.py`. La interfaz web se sirve desde un directorio de pruebas.
"""
