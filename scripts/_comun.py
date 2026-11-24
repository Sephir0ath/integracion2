"""Utilidades compartidas por los scripts de demo.

Los scripts corren **en el host** (python3, solo biblioteca estandar) y hablan
con el sistema por el borde publicado:

* las compras, por el gateway (`http://127.0.0.1:8080`);
* la inyeccion de fallas, por `docker compose exec`, porque los puertos admin
  de los servicios **no** estan publicados: viven solo en la red interna.

Todo se puede mover con variables de entorno:

    TICKETERA_API      base del gateway        (por defecto http://127.0.0.1:8080)
    TICKETERA_BFF      base del BFF            (por defecto http://127.0.0.1:8081)
    PROMETHEUS_URL     base de Prometheus       (por defecto http://127.0.0.1:9090)
    GRAFANA_URL        base de Grafana         (por defecto http://127.0.0.1:3000)
    TICKETERA_USUARIO  correo de demostracion  (por defecto cliente@ticketera.local)
    TICKETERA_CLAVE    contrasena              (por defecto cliente123)
"""

from __future__ import annotations

import json
import os
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
API = os.environ.get("TICKETERA_API", "http://127.0.0.1:8080").rstrip("/")
BFF = os.environ.get("TICKETERA_BFF", "http://127.0.0.1:8081").rstrip("/")
PROMETHEUS = os.environ.get("PROMETHEUS_URL", "http://127.0.0.1:9090").rstrip("/")
GRAFANA = os.environ.get("GRAFANA_URL", "http://127.0.0.1:3000").rstrip("/")
USUARIO = os.environ.get("TICKETERA_USUARIO", "cliente@ticketera.local")
CLAVE = os.environ.get("TICKETERA_CLAVE", "cliente123")

# Direcciones internas (con puerto) de los dos servicios que aceptan fallas.
PASARELA = "pasarela:9500"
EMISION = "emision:9003"


# --------------------------------------------------------------------- HTTP --
class Sesion:
    """La identidad del cliente: el token y lo que el gateway propaga.

    El gateway valida el JWT y reenvia `X-Client-Id` y `X-Role` a los
    servicios internos. El BFF no revalida el token, pero **exige** que
    venga `X-Client-Id`: por eso las llamadas al BFF necesitan la sesion
    completa y no solo el token.
    """

    def __init__(self, access_token: str, cliente_id: str, rol: str) -> None:
        self.access = access_token
        self.cliente_id = cliente_id
        self.rol = rol

    def cabeceras(self, con_identidad: bool = False) -> dict:
        cabeceras = {"Authorization": f"Bearer {self.access}"}
        if con_identidad:
            cabeceras["X-Client-Id"] = self.cliente_id
            cabeceras["X-Role"] = self.rol
        return cabeceras

def _pedir(metodo: str, url: str, cuerpo: dict | None, cabecera: str | None,
           extra: dict | None = None):
    """Hace la peticion y devuelve (codigo, cuerpo_json_o_texto, segundos)."""
    datos = json.dumps(cuerpo).encode() if cuerpo is not None else None
    cabeceras = {"Content-Type": "application/json"}
    if cabecera:
        cabeceras["Authorization"] = f"Bearer {cabecera}"
    cabeceras.update(extra or {})
    peticion = urllib.request.Request(url, data=datos, headers=cabeceras, method=metodo)
    inicio = time.perf_counter()
    try:
        with urllib.request.urlopen(peticion, timeout=30) as respuesta:
            crudo = respuesta.read().decode()
            codigo = respuesta.status
    except urllib.error.HTTPError as error:
        crudo = error.read().decode()
        codigo = error.code
    finally:
        segundos = time.perf_counter() - inicio
    try:
        return codigo, json.loads(crudo), segundos
    except json.JSONDecodeError:
        return codigo, {"_texto": crudo}, segundos


def token(usuario: str = USUARIO, clave: str = CLAVE) -> Sesion:
    """Pide un token de acceso con las credenciales de demostracion."""
    codigo, cuerpo, _ = _pedir("POST", f"{API}/api/v1/auth/token", {"email": usuario, "password": clave}, None)
    if codigo != 200:
        raise RuntimeError(f"no se pudo obtener el token ({codigo}): {cuerpo}")
    return Sesion(cuerpo["access_token"], cuerpo.get("cliente_id", ""), cuerpo.get("role", "cliente"))


def api(metodo: str, ruta: str, cuerpo: dict | None = None, sesion: Sesion | None = None):
    """Llama a la API del gateway y devuelve (codigo, cuerpo, segundos)."""
    token = sesion.access if isinstance(sesion, Sesion) else sesion
    return _pedir(metodo, f"{API}{ruta}", cuerpo, token)


def api_bff(metodo: str, ruta: str, cuerpo: dict | None = None, sesion: Sesion | None = None):
    """Llama a la API del BFF (la que usa la interfaz web).

    El BFF no revalida el token pero exige `X-Client-Id`: sin esa cabecera
    responde 401 (`NO_AUTENTICADO`).
    """
    if isinstance(sesion, Sesion):
        return _pedir(metodo, f"{BFF}{ruta}", cuerpo, sesion.access, sesion.cabeceras(con_identidad=True))
    return _pedir(metodo, f"{BFF}{ruta}", cuerpo, sesion)


# ------------------------------------------------------------------- fallas --
def fallar_fault(direccion: str, modo: str, delay_ms: int = 0, fail_rate: float = 0.0) -> dict:
    """Inyecta una falla desde adentro de la red de compose.

    `direccion` es el servicio con su puerto (`pasarela:9500`, `emision:9003`).
    """
    cuerpo = json.dumps({"mode": modo, "delay_ms": delay_ms, "fail_rate": fail_rate})
    codigo = (
        "import json, urllib.request as u;"
        f"peticion = u.Request('http://{direccion}/admin/fault', data={cuerpo!r}.encode(),"
        " headers={'Content-Type': 'application/json'}, method='POST');"
        "print(u.urlopen(peticion, timeout=5).read().decode())"
    )
    resultado = subprocess.run(
        ["docker", "compose", "exec", "-T", "compras", "python", "-c", codigo],
        cwd=RAIZ, capture_output=True, text=True, check=True,
    )
    return json.loads(resultado.stdout.strip())


def restaurar(direccion: str) -> None:
    """Devuelve el servicio al modo `ok`."""
    fallar_fault(direccion, "ok")


# ------------------------------------------------------------------ catalog --
def saga_id(prefijo: str) -> str:
    """Identificador unico de saga.

    Importa: el mismo `saga_id` con otra carga util es un **conflicto** de
    idempotencia, no una repeticion.
    """
    return f"{prefijo}-{uuid.uuid4().hex[:12]}"


def catalogo(sesion: Sesion) -> dict:
    """El catalogo tal como lo ve la interfaz web."""
    codigo, cuerpo, _ = api_bff("GET", "/bff/web/catalogo", None, sesion)
    if codigo != 200:
        raise RuntimeError(f"el catalogo respondio {codigo}: {cuerpo}")
    return cuerpo


def secciones_con_libres(sesion: Sesion) -> list[dict]:
    """Las secciones que tienen al menos una butaca libre."""
    datos = catalogo(sesion)
    secciones = datos.get("secciones") or []
    return [s for s in secciones if int(s.get("cantidad_disponible", 0) or 0) > 0]


def butacas_libres(sesion: Sesion, seccion_id: str) -> list[str]:
    """Los ids de las butacas libres de una seccion."""
    codigo, cuerpo, _ = api_bff("GET", f"/bff/web/secciones/{seccion_id}/butacas", None, sesion)
    if codigo != 200:
        raise RuntimeError(f"las butacas de {seccion_id} respondieron {codigo}: {cuerpo}")
    return [b["butaca_id"] for b in (cuerpo.get("butacas") or []) if b.get("estado") == "LIBRE"]


def elegir_butacas(sesion: Sesion, cantidad: int = 1) -> tuple[str, str, list[str]]:
    """Elige `cantidad` butacas libres de la primera seccion con sitio.

    Devuelve (evento_id, seccion_id, [butaca_id]). Nunca se hardcodean ids:
    los nombres de las butacas varian entre secciones (`A1-A8`, `B9-B16`...).
    """
    for seccion in secciones_con_libres(sesion):
        seccion_id = seccion["seccion_id"]
        libres = butacas_libres(sesion, seccion_id)
        if len(libres) >= cantidad:
            return seccion.get("evento_id", ""), seccion_id, libres[:cantidad]
    raise RuntimeError("no hay butacas libres suficientes en el catalogo")


# ----------------------------------------------------------------- compras --
def seccion_con_mas_libres(sesion: Sesion) -> tuple[str, str, list[str]]:
    """La seccion con mas butacas libres y sus ids.

    Importa para las rafagas: si cada compra busca su butaca por su cuenta,
    las llamadas se desfasan y la rafaga deja de ser una rafaga.
    """
    mejor = ("", "", [])
    for seccion in secciones_con_libres(sesion):
        seccion_id = seccion["seccion_id"]
        libres = butacas_libres(sesion, seccion_id)
        if len(libres) > len(mejor[2]):
            mejor = (seccion.get("evento_id", ""), seccion_id, libres)
    if not mejor[2]:
        raise RuntimeError("no hay butacas libres en el catalogo")
    return mejor


def comprar(sesion: Sesion, saga: str, evento_id: str, seccion_id: str, butacas: list[str]):
    """Ejecuta la compra y devuelve (codigo, saga, segundos)."""
    cuerpo = {
        "saga_id": saga,
        "evento_id": evento_id,
        "seccion_id": seccion_id,
        "butaca_ids": butacas,
    }
    codigo, respuesta, segundos = api("POST", "/api/v1/compras", cuerpo, sesion)
    return codigo, respuesta, segundos


def saga_de(respuesta: dict) -> dict:
    """La saga, venga envuelta en `compra` o suelta."""
    if isinstance(respuesta, dict) and "compra" in respuesta:
        return respuesta.get("compra") or {}
    return respuesta if isinstance(respuesta, dict) else {}


def entradas_de(sesion: Sesion, saga: str) -> list[dict]:
    """Las entradas emitidas por una saga."""
    codigo, cuerpo, _ = api("GET", f"/api/v1/entradas?saga_id={saga}", None, sesion)
    if codigo != 200 or not isinstance(cuerpo, dict):
        return []
    return cuerpo.get("entradas", []) or []


def pasos_de(saga: dict) -> str:
    """`RESERVAR OK · COBRAR ERROR · EMITIR SKIPPED`."""
    return " · ".join(f"{p.get('nombre')} {p.get('resultado')}" for p in saga.get("pasos", []))


def compensaciones_de(saga: dict) -> list[str]:
    """Los nombres de las compensaciones ejecutadas, en orden."""
    nombres = []
    for compensacion in saga.get("compensaciones", []) or []:
        nombre = compensacion.get("nombre") or compensacion.get("paso")
        if nombre:
            nombres.append(nombre)
    return nombres


# --------------------------------------------------------------- metricas --
def metrica(consulta: str) -> list[dict]:
    """Las series de una consulta de Prometheus."""
    url = f"{PROMETHEUS}/api/v1/query?query={urllib.parse.quote(consulta)}"
    try:
        with urllib.request.urlopen(url, timeout=10) as respuesta:
            datos = json.loads(respuesta.read().decode())
    except Exception:  # noqa: BLE001 - si Prometheus no esta, no hay metricas
        return []
    return datos.get("data", {}).get("result", []) or []


def valor_metrica(consulta: str) -> float:
    """El ultimo valor de la primera serie de una consulta."""
    series = metrica(consulta)
    if not series:
        return 0.0
    return float(series[0]["value"][1])


# --------------------------------------------------------------- presentacion --
class Demo:
    """Imprime el relato de la demo y lleva la cuenta de las comprobaciones."""

    def __init__(self, titulo: str) -> None:
        self.titulo = titulo
        self.fallos: list[str] = []
        print("=" * 72)
        print(f" {titulo}")
        print("=" * 72)

    def seccion(self, texto: str) -> None:
        print(f"\n[{texto}]")

    def dato(self, texto: str) -> None:
        print(f"      {texto}")

    def ok(self, condicion: bool, mensaje: str) -> bool:
        marca = "OK  " if condicion else "FALLA"
        print(f"      [{marca}] {mensaje}")
        if not condicion:
            self.fallos.append(mensaje)
        return condicion

    def cierre(self) -> int:
        print("\n" + "-" * 72)
        if self.fallos:
            print(f" RESULTADO: {len(self.fallos)} comprobacion(es) fallaron")
            for fallo in self.fallos:
                print(f"   - {fallo}")
            return 1
        print(" RESULTADO: todas las comprobaciones pasaron")
        return 0
