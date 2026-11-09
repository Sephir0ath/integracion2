# Front de la ticketera

Interfaz web de la boletería. Sin framework y sin paso de build: son HTML, CSS y
JavaScript que el BFF sirve como archivos estáticos.

## Cómo verlo sin el backend

```bash
cd frontend/web
python3 -m http.server 8099
# abrir http://127.0.0.1:8099/index.html
```

Sin la API el front funciona en **modo demostración**: usa datos de muestra, lo
avisa en la barra superior y marca la compra como simulación. No simula un cobro
real ni esconde que no hubo Saga.

Con el sistema levantado, la interfaz se sirve en `http://localhost:8081` y habla
con el gateway en `http://localhost:8080`.

## Archivos

| Archivo | Contenido |
|---|---|
| `index.html` | Estructura de las tres vistas: comprar, mis entradas, administrar |
| `styles.css` | Sistema de diseño: tokens, tipografías, componentes |
| `app.js` | Estado, cliente HTTP y render de cada vista |
| `config.js` | URL de la API y credenciales de la demostración |
| `fixtures.js` | Datos de muestra y generación determinista del plano de butacas |
| `fonts/` | Big Shoulders Display, Instrument Sans e IBM Plex Mono auto-alojadas |

Las tipografías van en el repo a propósito: la defensa puede ser sin internet y
la interfaz no puede depender de Google Fonts.

## Rutas que consume

El front **solo** habla con el API Gateway: es la única puerta de entrada al
sistema, así que nunca llama directo a un servicio interno.

| Ruta | Uso |
|---|---|
| `GET /bff/web/catalogo` | Eventos, secciones, métodos de pago y estado de degradación |
| `GET /bff/web/secciones/{seccion_id}/butacas` | Plano de butacas |
| `POST /bff/web/compras` | Compra (idempotente por `saga_id`) |
| `GET /bff/web/compras/{saga_id}` | Estado de la compra |
| `GET /api/v1/butacas/eventos` · `POST` · `PUT {id}` · `DELETE {id}` | CRUD de eventos |
| `GET /api/v1/butacas/secciones` · `POST` · `PUT {id}` · `DELETE {id}` | CRUD de secciones |

Las rutas de administracion exigen rol `admin` en el token.

## Decisiones de diseño

- **Concepto**: el programa impreso que dan en la puerta, al lado del plano de la
  sala. Tintas planas de serigrafia sobre papel de newsprint, sin degradados.
- **El plano es el heroe**: la sala se dibuja en tinta, la butaca ocupada es un
  bloque apagado y la elegida se llena de oxblood.
- **El stub** es el elemento unico en ambar, con la perforacion marcada.
- **Tipografia**: Big Shoulders Display para nombres y cifras, Instrument Sans
  para el texto, IBM Plex Mono solo dentro del stub (imita el ticket impreso).
- **Movimiento**: solo el que responde a la accion de la persona (la seleccion y
  la aparicion del stub). Se respeta `prefers-reduced-motion`.

## Pendiente para cuando exista el backend

1. El gateway debe responder con cabeceras CORS para el origen del BFF
   (`http://localhost:8081`), porque el front se sirve en un puerto y llama a la
   API en otro.
2. `POST /api/v1/auth/token` debe devolver `client_id` y `role`; el front guarda
   el token y lo envia en `Authorization`.
3. El QR real lo entrega `emision` en el campo `qr` de cada entrada. La
   plantilla dibuja una matriz de 21x21 derivada del identificador, que es la
   forma pero no un codigo escaneable.
