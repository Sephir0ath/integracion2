# Pruebas end-to-end (Playwright)

Verificación de la ticketera **con un navegador real** contra el sistema
dockerizado: la suite abre el Chromium, maneja la interfaz como lo haria una
persona y afirma lo que ve en pantalla y lo que quedo guardado en el sistema.

No es un doble del backend: lo que se prueba es la aplicacion terminada.

## Que hace falta

- **Node.js 18+** (el del entorno de desarrollo).
- **El sistema arriba**:

  ```bash
  docker compose up -d --build
  docker compose ps        # los 13 contenedores healthy
  ```

- Un Chromium. Por defecto usa el del sistema (`/usr/bin/chromium`,
  `/usr/bin/chromium-browser` o `/usr/bin/google-chrome`), asi que **no hace
  falta descargar el navegador de Playwright**. Si se quiere otro:

  ```bash
  E2E_CHROMIUM=/ruta/al/chromium npm test
  ```

## Como se corre

```bash
cd tests/e2e
npm install                # solo @playwright/test (no descarga navegadores)

npm test                   # headless: lo normal para verificar
npm run test:headed        # con el navegador a la vista
npm test -- specs/compra.spec.js        # una sola spec
npm test -- --grep "compra"             # por nombre de prueba
```

| Variable | Para que sirve |
|---|---|
| `HEADED=1` | navegador visible (lo usa `npm run test:headed`) |
| `E2E_UI_BASE` | URL de la interfaz (por defecto `http://127.0.0.1:8081`) |
| `E2E_API_BASE` | URL del gateway (por defecto `http://127.0.0.1:8080`) |
| `E2E_CHROMIUM` | ruta del navegador |
| `E2E_LIMITE=1` | habilita la prueba del limite de peticiones (ver abajo) |
| `E2E_TMPDIR` | directorio temporal del navegador (por defecto `/tmp`) |

La suite usa `/tmp` como temporal porque Chromium crea un socket de unicidad
en `TMPDIR` y, con una ruta larga, el arranque falla con
`Socket path too long`.

## Que cubre

| Spec | Que verifica |
|---|---|
| `specs/compra.spec.js` | El camino central: entrar, elegir evento, elegir butacas, ver el total, comprar y recibir las entradas. Afirma que la saga queda `CONFIRMADA` con sus tres pasos en orden, que el monto es el esperado y que **la butaca comprada ya no está libre** para el siguiente comprador. |
| `specs/administracion.spec.js` | La pantalla de administracion: el formulario abre y cierra bien, crear, editar y eliminar eventos, crear secciones, y que la tabla **se actualiza sin recargar la pagina**. |
| `specs/resiliencia.spec.js` | Que la interfaz degrada bien: gateway caido, login rechazado, un 401 que dispara el re-login, y el catalogo degradado sin pagos. |

## Decisiones de diseño (y por que)

- **No toca el entorno.** La suite no levanta ni baja contenedores, no reinicia
  servicios y no modifica el `docker-compose`: corre contra el sistema que ya
  esta arriba. Puede convivir con los compañeros trabajando.
- **No deja basura.** Cada prueba crea lo que necesita y lo borra en un
  `finally`, aunque falle. Al final, el catalogo queda como estaba.
  Ojo: el dominio **no** permite borrar un evento con butacas vendidas (una
  venta deja rastro), asi que cuando la API rechaza el borrado, la limpieza
  de la prueba se hace directo en la base de `butacas` (por el orden de las
  claves foraneas: butacas, secciones y evento). Es limpieza de *fixture*,
  no parte del sistema que se esta probando.
- **Serial** (`workers: 1`). El gateway limita a **60 peticiones por minuto y
  cliente** (`RATE_LIMIT_PER_MINUTE`, burst 20) y las pruebas comparten el
  usuario de demostracion: en paralelo, varias pruebas se expulsarian entre
  si con un 429.
- **No depende de datos de corridas anteriores.** Cada prueba crea su propio
  evento con butacas libres.
- **Espera por asercion, no por reloj.** No hay `sleep`: las aserciones de
  Playwright reintentan solas hasta el limite.
- **El limite de peticiones (429) va aparte.** Drena el cubo del cliente y
  romperia el resto de la corrida, asi que vive en un `describe` etiquetado
  `@limite`, **omitido por defecto**: se activa con `E2E_LIMITE=1` y conviene
  correrlo solo.

## Cuando falla

Playwright deja la evidencia en `tests/e2e/test-results/`:

- `test-failed-1.png`: la pantalla en el momento del fallo;
- `trace.zip`: la traza completa, para reproducirla con
  `npx playwright show-trace test-results/.../trace.zip`;
- `error-context.md`: el arbol de accesibilidad de la pagina.

Esos archivos estan en `.gitignore` (`node_modules/`, `test-results/`,
`playwright-report/`).
