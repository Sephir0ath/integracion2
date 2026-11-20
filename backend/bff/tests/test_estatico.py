"""Pruebas de la interfaz web estatica.

El BFF es el unico contenedor que lleva la interfaz (el encargo exige que
solo el gateway y el BFF queden expuestos), asi que la sirve en `/` como
archivos estaticos, con `index.html` como portada.
"""

from __future__ import annotations


async def test_la_interfaz_estatica_se_sirve_en_la_raiz(http):
    """`GET /` devuelve la portada de la interfaz."""
    respuesta = await http.get("/")

    assert respuesta.status_code == 200
    assert respuesta.headers["content-type"].startswith("text/html")
    assert "Ticketera" in respuesta.text


async def test_los_archivos_de_la_interfaz_se_sirven_con_su_tipo(http):
    """Cada archivo se sirve con su propio tipo de contenido."""
    respuesta = await http.get("/estilos.css")

    assert respuesta.status_code == 200
    assert respuesta.headers["content-type"].startswith("text/css")
    assert "#14303A" in respuesta.text


async def test_la_interfaz_no_exige_la_identidad_del_cliente(http):
    """La interfaz es publica: la pide el navegador, sin token."""
    assert (await http.get("/")).status_code == 200
