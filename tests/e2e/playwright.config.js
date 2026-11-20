// Configuracion de las pruebas end-to-end de la ticketera.
//
// La suite corre contra el sistema **ya dockerizado** (no levanta nada):
// la interfaz la sirve el BFF en :8081 y el gateway en :8080.
//
//   npm test                 -> headless (rapido, para verificar)
//   npm run test:headed      -> con navegador visible
//
// Por defecto se usa el Chromium del sistema (`/usr/bin/chromium`), asi que
// no hace falta descargar el navegador de Playwright. Si el Chromium de
// Playwright esta instalado, se usa ese: se comprueba primero la cache.

const fs = require('node:fs');
const { defineConfig } = require('@playwright/test');

// Chromium crea un socket de unicidad en TMPDIR y con una ruta larga el
// arranque falla con "Socket path too long". El TMPDIR de los entornos de
// desarrollo suele ser largo, asi que la suite usa /tmp salvo que se diga
// otra cosa con E2E_TMPDIR.
if (!process.env.E2E_TMPDIR) {
  process.env.TMPDIR = '/tmp';
}

const UI = process.env.E2E_UI_BASE || 'http://127.0.0.1:8081';
const headed = process.env.HEADED === '1';

function navegadorDelSistema() {
  if (process.env.E2E_CHROMIUM) return process.env.E2E_CHROMIUM;
  const candidatos = ['/usr/bin/chromium', '/usr/bin/chromium-browser', '/usr/bin/google-chrome'];
  for (const ruta of candidatos) {
    if (fs.existsSync(ruta)) return ruta;
  }
  // Sin navegador del sistema: que Playwright use el suyo (si esta instalado).
  return undefined;
}

const ejecutable = navegadorDelSistema();

module.exports = defineConfig({
  testDir: './specs',
  // La compra recorre el gateway, el BFF y la saga: son varias llamadas.
  timeout: 120_000,
  expect: { timeout: 20_000 },
  // Serial: el gateway limita las peticiones por cliente (60 por minuto) y
  // las pruebas comparten el mismo usuario de demostracion.
  fullyParallel: false,
  workers: 1,
  retries: 0,
  reporter: [['list']],
  use: {
    baseURL: UI,
    headless: !headed,
    viewport: { width: 1400, height: 1000 },
    trace: 'retain-on-failure',
    screenshot: 'only-on-failure',
    video: 'off',
    launchOptions: {
      args: ['--no-sandbox', '--disable-dev-shm-usage'],
      ...(ejecutable ? { executablePath: ejecutable } : {}),
    },
  },
  projects: [{ name: 'chromium', use: { browserName: 'chromium' } }],
});
