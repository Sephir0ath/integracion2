"""Limite de peticiones por cliente: tazon de fichas (token bucket).

Cada cliente tiene su propio tazon:

* la **capacidad** es el burst (`RATE_LIMIT_BURST`, 20 por defecto): es
  decir, quantas peticiones seguidas se permiten;
* la **recarga** es el limite por minuto (`RATE_LIMIT_PER_MINUTE`, 60 por
  defecto): el tazon se vuelve a llenar a esa velocidad.

El estado del tazon vive en **Redis** (`REDIS_URL`), que es lo que hace que
el limite valga entre varias replicas del gateway. La operacion es
atomica: se hace con un script de Lua, que Redis ejecuta entero, sin que
otra peticion se meta en medio. Dos peticiones concurrentes del mismo
cliente no pueden pasar las dos el limite.

**Si Redis no responde el gateway sigue sirviendo**: el limitador se
apoya en un tazon en memoria del proceso y sigue limitando. La caida de
Redis no puede tumbar el sistema, y el respaldo tiene un tiempo de espera
para no castigar la latencia con un fallo de red en cada peticion.
"""

from __future__ import annotations

import asyncio
import logging
import math
import time
from dataclasses import dataclass

from common.logging import log_event

logger = logging.getLogger("gateway.rate_limit")

# Script de Lua del tazon: Redis lo ejecuta entero, asi que el leer, el
# recargar y el descontar son una sola operacion atomica.
#
# ARGV: 1) capacidad  2) recarga por segundo  3) costo  4) ttl en ms
# Devuelve: {permitido (1|0), fichas restantes}
ESCRIPT_TAZON = """
local clave = KEYS[1]
local capacidad = tonumber(ARGV[1])
local recarga_por_segundo = tonumber(ARGV[2])
local costo = tonumber(ARGV[3])
local ttl_milisegundos = tonumber(ARGV[4])

local reloj = redis.call('TIME')
local ahora = tonumber(reloj[1]) + (tonumber(reloj[2]) / 1000000)

local datos = redis.call('HMGET', clave, 'tokens', 'ts')
local tokens = tonumber(datos[1])
local ts = tonumber(datos[2])
if tokens == nil or ts == nil then
  tokens = capacidad
  ts = ahora
end

if ahora > ts then
  tokens = math.min(capacidad, tokens + (ahora - ts) * recarga_por_segundo)
end
ts = ahora

local permitido = 0
if tokens >= costo then
  tokens = tokens - costo
  permitido = 1
end

redis.call('HSET', clave, 'tokens', tostring(tokens), 'ts', tostring(ts))
redis.call('PEXPIRE', clave, ttl_milisegundos)
return {permitido, tostring(tokens)}
"""

# Prefijo de las claves del limitador en Redis.
PREFIJO_CLAVE = "gateway:limite:"

# Cuanto se espera antes de volver a intentar Redis despues de un fallo.
ESPERA_REDIS_CAIDO = 5.0


@dataclass(frozen=True)
class Decision:
    """La respuesta del limitador para una peticion."""

    permitido: bool
    reintento_tras: int = 0
    almacen: str = "memoria"


def consumir_tokens(
    tokens: float,
    ts: float,
    ahora: float,
    capacidad: float,
    recarga_por_segundo: float,
    costo: float = 1.0,
) -> tuple[bool, float, float]:
    """La cuenta del tazon: recarga, descuenta y dice si pasa.

    Esta funcion es la **referencia del script de Lua**: el script hace
    exactamente esta cuenta sobre el reloj del servidor de Redis y el
    respaldo en memoria la hace con el reloj del proceso. Las unidades
    son segundos y fichas.

    Devuelve `(permitido, fichas_restantes, marca_de_tiempo)`.
    """
    if ahora > ts:
        tokens = min(capacidad, tokens + (ahora - ts) * recarga_por_segundo)
    ts = ahora
    if tokens >= costo:
        return True, tokens - costo, ts
    return False, tokens, ts


class LimitadorTazon:
    """Limite de peticiones por cliente, con Redis y respaldo en memoria."""

    def __init__(
        self,
        redis=None,
        capacidad: int = 20,
        recarga_por_minuto: int = 60,
        espera_redis_caido: float = ESPERA_REDIS_CAIDO,
        reloj=time.time,
    ) -> None:
        self._redis = redis
        self._capacidad = float(capacidad)
        self._recarga_por_segundo = float(recarga_por_minuto) / 60.0
        self._espera_redis_caido = float(espera_redis_caido)
        self._reloj = reloj
        self._cubos: dict[str, tuple[float, float]] = {}
        self._candado = asyncio.Lock()
        self._redis_caido_hasta = 0.0

    @property
    def capacidad(self) -> float:
        """Fichas maxima de un tazon."""
        return self._capacidad

    @property
    def recarga_por_segundo(self) -> float:
        """Fichas que se recuperan por segundo."""
        return self._recarga_por_segundo

    async def permitir(self, cliente_id: str) -> Decision:
        """Consume una ficha del tazon del cliente."""
        if self._redis is not None and self._reloj() >= self._redis_caido_hasta:
            decision = await self._consumir_en_redis(cliente_id)
            if decision is not None:
                return decision
        return await self._consumir_en_memoria(cliente_id)

    # ------------------------------------------------------------ redis --
    async def _consumir_en_redis(self, cliente_id: str) -> Decision | None:
        """Consume la ficha en Redis, o `None` si Redis no responde."""
        clave = f"{PREFIJO_CLAVE}{cliente_id}"
        try:
            respuesta = await self._redis.eval(
                ESCRIPT_TAZON,
                1,
                clave,
                self._capacidad,
                self._recarga_por_segundo,
                1.0,
                self._ttl_milisegundos(),
            )
        except Exception as error:  # noqa: BLE001 - Redis caido no tumba el gateway
            self._redis_caido_hasta = self._reloj() + self._espera_redis_caido
            log_event(
                logger,
                "gateway_redis_caido",
                error=str(error),
                cliente_id=cliente_id,
                motivo="se sigue limitando con el tazon en memoria",
            )
            return None

        permitido = bool(int(_a_texto(respuesta[0])))
        fichas = float(_a_texto(respuesta[1]))
        return Decision(
            permitido=permitido,
            reintento_tras=self._reintento(fichas),
            almacen="redis",
        )

    def _ttl_milisegundos(self) -> int:
        """Tiempo de vida de la clave: alcanza para llenar el tazon."""
        segundos = self._capacidad / max(self._recarga_por_segundo, 0.001) + 60.0
        return int(segundos * 1000)

    # ---------------------------------------------------------- memoria --
    async def _consumir_en_memoria(self, cliente_id: str) -> Decision:
        """Consume la ficha en el tazon del proceso."""
        async with self._candado:
            ahora = self._reloj()
            fichas, marca = self._cubos.get(cliente_id, (self._capacidad, ahora))
            permitido, fichas, marca = consumir_tokens(
                fichas,
                marca,
                ahora,
                self._capacidad,
                self._recarga_por_segundo,
            )
            self._cubos[cliente_id] = (fichas, marca)
        return Decision(
            permitido=permitido,
            reintento_tras=self._reintento(fichas),
            almacen="memoria",
        )

    def _reintento(self, fichas: float) -> int:
        """Segundos que hay que esperar para la siguiente ficha."""
        if fichas >= 1.0:
            return 0
        faltan = 1.0 - fichas
        return max(1, math.ceil(faltan / max(self._recarga_por_segundo, 0.001)))


def _a_texto(valor) -> str:
    """Redis puede devolver bytes o texto segun como este configurado."""
    if isinstance(valor, bytes):
        return valor.decode("utf-8")
    return str(valor)
