// =========================================================================
// Entorno de pruebas del front: un DOM mínimo y un `fetch` programable.
//
// El front no tiene build step ni framework, así que estas pruebas corren
// con el runner que trae Node (`node --test`) contra el mismo `app.js` que
// carga el navegador. Este archivo arma lo poco que el front necesita:
// `document`, `window.localStorage`, `window.crypto` y `fetch`.
//
//   node --test frontend/web/tests/
// =========================================================================
'use strict';

const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const RAIZ = path.resolve(__dirname, '..');

/** Nodo de DOM con lo que el front usa: hijos, atributos y oyentes. */
function crearNodo(etiqueta, campos) {
  const nodo = {
    tagName: String(etiqueta || 'div').toUpperCase(),
    className: '',
    textContent: '',
    id: '',
    type: '',
    value: '',
    title: '',
    disabled: false,
    hidden: false,
    style: {},
    dataset: {},
    atributos: {},
    hijos: [],
    oyentes: {},
    appendChild(hijo) {
      this.hijos.push(hijo);
      return hijo;
    },
    addEventListener(tipo, fn) {
      (this.oyentes[tipo] = this.oyentes[tipo] || []).push(fn);
    },
    removeEventListener() {},
    setAttribute(nombre, valor) {
      this.atributos[nombre] = String(valor);
    },
    getAttribute(nombre) {
      return this.atributos[nombre] === undefined ? null : this.atributos[nombre];
    },
    removeAttribute(nombre) {
      delete this.atributos[nombre];
    },
    async dispatch(tipo, evento) {
      const oyentes = this.oyentes[tipo] || [];
      let resultado;
      for (const fn of oyentes) {
        resultado = await fn(evento || { type: tipo, target: this, preventDefault() {} });
      }
      return resultado;
    },
  };
  for (const [nombre, valor] of Object.entries(campos || {})) nodo[nombre] = valor;
  return nodo;
}

/** Los campos que el front lee de los formularios de administración. */
function camposDelFormulario(selector) {
  if (selector === '#formulario-evento') {
    return { nombre: { value: '' }, lugar: { value: '' }, fecha: { value: '' } };
  }
  if (selector === '#formulario-seccion') {
    return { nombre: { value: '' }, capacidad_total: { value: '' }, precio: { value: '' } };
  }
  return {};
}

/** `document` mínimo: crea un nodo por selector y guarda los oyentes. */
function crearDocumento() {
  const porSelector = new Map();
  const oyentes = {};
  return {
    oyentes,
    createElement: (etiqueta) => crearNodo(etiqueta),
    createTextNode: (texto) => ({ tipo: 'texto', texto: String(texto) }),
    querySelector(selector) {
      if (!porSelector.has(selector)) {
        porSelector.set(selector, crearNodo('div', camposDelFormulario(selector)));
      }
      return porSelector.get(selector);
    },
    querySelectorAll() {
      return [];
    },
    addEventListener(tipo, fn) {
      (oyentes[tipo] = oyentes[tipo] || []).push(fn);
    },
    async dispatch(tipo, evento) {
      let resultado;
      for (const fn of oyentes[tipo] || []) {
        resultado = await fn(evento || { type: tipo, preventDefault() {} });
      }
      return resultado;
    },
  };
}

function crearRespuesta(estado, cuerpo) {
  return {
    ok: estado >= 200 && estado < 300,
    status: estado,
    async json() {
      return cuerpo;
    },
    async text() {
      return typeof cuerpo === 'string' ? cuerpo : JSON.stringify(cuerpo);
    },
  };
}

/**
 * `fetch` programable: cada llamada se registra y el programa decide la
 * respuesta. Si el programa devuelve un `Error`, la llamada falla (como si
 * la API no respondiera).
 */
function crearFetch(programa, pendientes) {
  const llamadas = [];
  const fetchStub = (url, opciones) => {
    const op = opciones || {};
    const llamada = {
      url: String(url),
      metodo: (op.method || 'GET').toUpperCase(),
      // Copia de las cabeceras: el front reutiliza el mismo objeto cuando
      // reintenta, y la llamada registrada debe quedar como se envió.
      cabeceras: Object.assign({}, op.headers),
      cuerpo: op.body === undefined ? null : op.body,
    };
    llamadas.push(llamada);
    const promesa = (async () => {
      const salida = await programa(llamada);
      if (salida instanceof Error) throw salida;
      return crearRespuesta(salida.estado, salida.cuerpo);
    })();
    pendientes.add(promesa);
    return promesa.finally(() => pendientes.delete(promesa));
  };
  return { fetchStub, llamadas };
}

/**
 * Carga `config.js`, `fixtures.js` y `app.js` en un contexto de vm con un
 * DOM mínimo, y devuelve el front listo para usar.
 */
function cargarFront(programa) {
  const documento = crearDocumento();
  const pendientes = new Set();
  const { fetchStub, llamadas } = crearFetch(programa, pendientes);
  const almacen = new Map();

  const ventana = {
    document: documento,
    localStorage: {
      getItem: (clave) => (almacen.has(clave) ? almacen.get(clave) : null),
      setItem: (clave, valor) => {
        almacen.set(clave, String(valor));
      },
      removeItem: (clave) => {
        almacen.delete(clave);
      },
    },
    crypto: {
      randomUUID: () => 'uuid-' + Math.random().toString(16).slice(2),
    },
    confirm: () => true,
  };

  const contexto = {
    window: ventana,
    document: documento,
    console: console,
    fetch: fetchStub,
    setTimeout: setTimeout,
    clearTimeout: clearTimeout,
  };
  vm.createContext(contexto);

  for (const archivo of ['config.js', 'fixtures.js', 'app.js']) {
    const codigo = fs.readFileSync(path.join(RAIZ, archivo), 'utf8');
    vm.runInContext(codigo, contexto, { filename: archivo });
  }

  return {
    ventana: ventana,
    documento: documento,
    contexto: contexto,
    llamadas: llamadas,
    config: ventana.TICKETERA,
    fixtures: ventana.FIXTURES,
    app: ventana.TICKETERA_APP,
    /** Arranca la interfaz como lo hace el navegador. */
    async iniciar() {
      await documento.dispatch('DOMContentLoaded');
    },
    /**
     * Deja terminar las peticiones que un manejador dejó en vuelo (los
     * escuchadores del DOM no devuelven la promesa que abren).
     */
    async asentar() {
      for (let vuelta = 0; vuelta < 50; vuelta += 1) {
        await new Promise((resolve) => setTimeout(resolve, 0));
        const enVuelo = Array.from(pendientes);
        if (!enVuelo.length) return;
        await Promise.allSettled(enVuelo);
      }
    },
    /** Las llamadas al gateway, en orden. */
    peticiones(ruta) {
      return llamadas.filter((llamada) => llamada.url.indexOf(ruta) !== -1);
    },
  };
}

module.exports = { cargarFront, crearRespuesta, crearNodo };
