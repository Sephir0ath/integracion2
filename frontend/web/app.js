/* =========================================================================
   Ticketera — lógica del front.

   La interfaz habla únicamente con el API Gateway (la puerta de entrada del
   sistema) y entra a él con un token de verdad: lo pide en
   `POST /api/v1/auth/token` (de forma perezosa, la primera vez que hace
   falta) y lo manda en la cabecera `Authorization`. Usa dos identidades: la
   de **cliente** para las rutas del BFF (`/bff/web/**`: catálogo, plano de la
   sala, compra y consulta de la saga) y la de **admin** para el CRUD del
   recinto (`/api/v1/butacas/**`), que el gateway solo abre con rol admin.

   Si la API no responde o el inicio de sesión falla y `modoDemo` está
   activo, la pantalla funciona con datos de muestra y lo dice: nunca simula
   un éxito que el backend no devolvió. Un 401 no es "el backend está caído":
   se renueva el token y se reintenta **una** vez.
   ========================================================================= */
(function () {
  'use strict';

  const cfg = window.TICKETERA;
  const FIX = window.FIXTURES;

  const estado = {
    vista: 'comprar',
    token: null,
    tokenAdmin: null,
    usuario: null,
    esAdmin: false,
    modoDemo: false,
    motivoDemo: null,
    aviso: null,
    eventos: [],
    secciones: [],
    metodosPago: [],
    eventoId: null,
    seccionId: null,
    butacas: [],
    seleccion: new Set(),
    entradas: [],
    editandoEvento: null,
    editandoSeccion: null,
    eventoDeSecciones: null,
  };

  /* ------------------------------------------------------------- utilidades */
  const $ = (selector) => document.querySelector(selector);

  function crear(etiqueta, clase, texto) {
    const nodo = document.createElement(etiqueta);
    if (clase) nodo.className = clase;
    if (texto !== undefined && texto !== null) nodo.textContent = String(texto);
    return nodo;
  }

  const pesos = new Intl.NumberFormat('es-CL', {
    style: 'currency',
    currency: 'CLP',
    maximumFractionDigits: 0,
  });

  function monto(centavos) {
    // Los precios viajan en centavos; el formateador espera pesos.
    return pesos.format(Math.round((centavos || 0) / 100));
  }

  function fechaLarga(iso) {
    const d = new Date(iso);
    if (Number.isNaN(d.getTime())) return iso || '';
    return d.toLocaleDateString('es-CL', { weekday: 'long', day: 'numeric', month: 'long', year: 'numeric' });
  }

  function fechaCorta(iso) {
    const d = new Date(iso);
    if (Number.isNaN(d.getTime())) return { dia: '--', mes: '' };
    return {
      dia: String(d.getDate()).padStart(2, '0'),
      mes: d.toLocaleDateString('es-CL', { month: 'short' }).replace('.', ''),
    };
  }

  function hora(iso) {
    const d = new Date(iso);
    if (Number.isNaN(d.getTime())) return '';
    return d.toLocaleTimeString('es-CL', { hour: '2-digit', minute: '2-digit' });
  }

  function idUnico() {
    if (window.crypto && window.crypto.randomUUID) return window.crypto.randomUUID();
    return 'saga-' + Math.random().toString(16).slice(2) + Date.now().toString(16);
  }

  function aviso(texto, nivel) {
    estado.aviso = texto ? { texto, nivel: nivel || 'info' } : null;
    const nodo = $('#aviso');
    if (!estado.aviso) {
      nodo.hidden = true;
      nodo.textContent = '';
      return;
    }
    nodo.hidden = false;
    nodo.dataset.nivel = estado.aviso.nivel;
    nodo.textContent = estado.aviso.texto;
  }

  /* ------------------------------------------------------- autenticacion */
  // La interfaz entra al sistema con dos identidades, porque el gateway
  // distingue los roles: el token de **cliente** para las rutas del BFF
  // (`/bff/web/**`) y el de **admin** para el CRUD del recinto
  // (`/api/v1/butacas/**`), que con otro rol responde 403.
  //
  // El token se pide de forma perezosa (la primera vez que hace falta) y se
  // reutiliza mientras siga vigente; la vida la dice el gateway en
  // `expires_in`. Las credenciales viven en `config.js`, no en el código.

  const expiraciones = { cliente: 0, admin: 0 };
  const loginsEnCurso = { cliente: null, admin: null };
  const MARGEN_EXPIRACION_MS = 5000;

  // Por qué se cayó al modo demo: el inicio de sesión falló, la API rechazó
  // la sesión o la API no respondió. No es lo mismo, y el aviso lo dice.
  const AVISOS_DEMO = {
    login: 'No se pudo iniciar sesión en la API: se muestran datos de demostración.',
    'no-autorizado': 'La API rechazó la sesión (401): se muestran datos de demostración.',
    'sin-respuesta': 'Sin respuesta de la API: se muestran datos de demostración.',
  };

  function credencialesDe(identidad) {
    if (identidad === 'admin') return { email: cfg.correoAdmin, password: cfg.claveAdmin };
    return { email: cfg.correoCliente, password: cfg.claveCliente };
  }

  function guardarToken(identidad, datos) {
    if (identidad === 'admin') {
      estado.tokenAdmin = datos.access_token;
      estado.esAdmin = true;
    } else {
      estado.token = datos.access_token;
      estado.usuario = {
        email: credencialesDe('cliente').email,
        rol: datos.role || 'cliente',
        cliente_id: datos.cliente_id || '',
      };
    }
    expiraciones[identidad] = Date.now() + (Number(datos.expires_in) || 3600) * 1000;
    if (identidad !== 'admin') mostrarSesion();
    return datos.access_token;
  }

  function tokenVigente(identidad) {
    const token = identidad === 'admin' ? estado.tokenAdmin : estado.token;
    if (!token) return null;
    // Se renueva un poco antes de expirar: el token vive una hora, pero el
    // reloj del navegador y el del gateway no tienen por qué coincidir.
    if (Date.now() >= expiraciones[identidad] - MARGEN_EXPIRACION_MS) return null;
    return token;
  }

  async function pedirToken(identidad) {
    const { email, password } = credencialesDe(identidad);
    let respuesta;
    try {
      respuesta = await fetch(cfg.apiBase + '/api/v1/auth/token', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ email, password }),
      });
    } catch (error) {
      // El gateway no respondió: no es un problema de credenciales, es que
      // la API no está.
      const fallo = new Error('el gateway no respondió: ' + error.message);
      fallo.origen = 'sin-respuesta';
      throw fallo;
    }
    if (!respuesta.ok) {
      const fallo = new Error('el gateway rechazó las credenciales (HTTP ' + respuesta.status + ')');
      fallo.origen = 'login';
      fallo.status = respuesta.status;
      throw fallo;
    }
    const datos = await respuesta.json();
    if (!datos || !datos.access_token) {
      const fallo = new Error('el gateway no devolvió un token');
      fallo.origen = 'login';
      throw fallo;
    }
    return guardarToken(identidad, datos);
  }

  // El token se pide una sola vez mientras siga vigente. Si dos pedidos lo
  // necesitan al mismo tiempo, ambos esperan al mismo inicio de sesión.
  async function obtenerToken(identidad, forzar) {
    if (!forzar) {
      const vigente = tokenVigente(identidad);
      if (vigente) return vigente;
      if (loginsEnCurso[identidad]) return loginsEnCurso[identidad];
    }
    const promesa = pedirToken(identidad);
    loginsEnCurso[identidad] = promesa;
    try {
      return await promesa;
    } finally {
      if (loginsEnCurso[identidad] === promesa) loginsEnCurso[identidad] = null;
    }
  }

  function motivoDe(error) {
    if (error && error.origen === 'login') return 'login';
    if (error && error.origen === 'sin-respuesta') return 'sin-respuesta';
    if (error && error.status === 401) return 'no-autorizado';
    return 'sin-respuesta';
  }

  /* ------------------------------------------------------------------ HTTP */
  // Las rutas de escritura del recinto las atiende el gateway solo con rol
  // admin: con el token de cliente responden 403.
  function identidadDe(ruta, metodo) {
    const verbo = (metodo || 'GET').toUpperCase();
    if (verbo !== 'GET' && verbo !== 'HEAD' && ruta.indexOf('/api/v1/butacas/') === 0) return 'admin';
    return 'cliente';
  }

  async function pedir(ruta, opciones) {
    const config = Object.assign({ headers: {} }, opciones || {});
    config.headers = Object.assign({ 'Content-Type': 'application/json' }, config.headers);
    const identidad = config.identidad || identidadDe(ruta, config.method);
    delete config.identidad; // no es una opción de `fetch`
    return pedirConReintento(ruta, config, identidad, true);
  }

  async function pedirConReintento(ruta, config, identidad, permitirReintento) {
    const token = await obtenerToken(identidad, false);
    if (token) config.headers.Authorization = 'Bearer ' + token;

    const respuesta = await fetch(cfg.apiBase + ruta, config);
    if (respuesta.status === 401 && permitirReintento) {
      // El token venció (vive una hora) o el gateway se reinició con otro
      // secreto: se pide uno nuevo y se reintenta **una** vez. Un 401 no es
      // "el backend está caído". El cuerpo es un texto (JSON serializado),
      // así que el reintento puede repetir la misma petición.
      await obtenerToken(identidad, true);
      return pedirConReintento(ruta, config, identidad, false);
    }
    if (!respuesta.ok) {
      const cuerpo = await respuesta.text();
      let detalle = cuerpo;
      try {
        const objeto = JSON.parse(cuerpo);
        detalle = objeto.detail || objeto.message || cuerpo;
      } catch (error) {
        /* el cuerpo no es JSON: se muestra tal cual */
      }
      const fallo = new Error(typeof detalle === 'string' ? detalle : 'Error ' + respuesta.status);
      fallo.status = respuesta.status;
      throw fallo;
    }
    if (respuesta.status === 204) return null;
    return respuesta.json();
  }

  /* --------------------------------------------------- almacén local (demo) */
  const CLAVE_LOCAL = 'ticketera.datos';

  function leerLocal() {
    try {
      return JSON.parse(window.localStorage.getItem(CLAVE_LOCAL)) || null;
    } catch (error) {
      return null;
    }
  }

  function guardarLocal() {
    try {
      window.localStorage.setItem(
        CLAVE_LOCAL,
        JSON.stringify({
          eventos: estado.eventos,
          secciones: estado.secciones,
          butacas: estado.butacasPorSeccion,
          entradas: estado.entradas,
        })
      );
    } catch (error) {
      /* sin almacenamiento disponible: la sesión sigue en memoria */
    }
  }

  function cargarDemo() {
    const guardado = leerLocal();
    estado.eventos = guardado && guardado.eventos ? guardado.eventos : FIX.eventos.slice();
    estado.secciones = guardado && guardado.secciones ? guardado.secciones : FIX.secciones.slice();
    estado.butacasPorSeccion = guardado && guardado.butacas ? guardado.butacas : FIX.butacas;
    estado.entradas = guardado && guardado.entradas ? guardado.entradas : [];
    estado.metodosPago = FIX.metodos_pago;
  }

  /* --------------------------------------------------------------- repositorio */
  const repo = {
    async catalogo() {
      try {
        const datos = await pedir('/bff/web/catalogo');
        estado.modoDemo = false;
        estado.motivoDemo = null;
        estado.eventos = datos.eventos || [];
        estado.secciones = datos.secciones || [];
        estado.metodosPago = datos.metodos_pago || [];
        estado.pagosDisponible = datos.pagos_disponible !== false;
        return;
      } catch (error) {
        if (!cfg.modoDemo) throw error;
        estado.modoDemo = true;
        estado.motivoDemo = motivoDe(error);
        estado.pagosDisponible = true;
        cargarDemo();
      }
    },

    async butacas(seccionId) {
      try {
        const datos = await pedir('/bff/web/secciones/' + encodeURIComponent(seccionId) + '/butacas');
        estado.modoDemo = false;
        return datos.butacas || [];
      } catch (error) {
        if (!cfg.modoDemo) throw error;
        estado.modoDemo = true;
        estado.motivoDemo = motivoDe(error);
        return (estado.butacasPorSeccion[seccionId] || []).slice();
      }
    },

    async comprar(payload) {
      if (estado.modoDemo) {
        // Sin backend no hay Saga que ejecutar: se simula la compra localmente
        // y queda etiquetada como simulación, para no parecer un cobro real.
        const seccion = estado.secciones.find((s) => s.seccion_id === payload.seccion_id);
        const candidatas = estado.butacasPorSeccion[payload.seccion_id] || [];
        const seleccion = payload.butaca_ids && payload.butaca_ids.length
          ? candidatas.filter((b) => payload.butaca_ids.includes(b.butaca_id))
          : [];
        const total = (seccion ? seccion.precio_centavos : 0) * (payload.butaca_ids || []).length;
        return {
          simulado: true,
          compra: {
            saga_id: payload.saga_id,
            estado: 'CONFIRMADA',
            monto_total_centavos: total,
            moneda: 'CLP',
          },
          entradas: seleccion.map((b, indice) => ({
            entrada_id: payload.saga_id + '-' + (indice + 1),
            saga_id: payload.saga_id,
            butaca_id: b.butaca_id,
            fila: b.fila,
            numero: b.numero,
            qr: payload.saga_id + ':' + b.butaca_id,
            estado: 'EMITIDA',
          })),
        };
      }
      const datos = await pedir('/bff/web/compras', {
        method: 'POST',
        body: JSON.stringify(payload),
      });
      estado.modoDemo = false;
      return datos;
    },

    async compra(sagaId) {
      const datos = await pedir('/bff/web/compras/' + encodeURIComponent(sagaId));
      estado.modoDemo = false;
      return datos;
    },

    async crearEvento(datos) {
      if (estado.modoDemo) {
        const evento = Object.assign({ evento_id: idUnico() }, datos);
        estado.eventos.push(evento);
        guardarLocal();
        return evento;
      }
      return pedir('/api/v1/butacas/eventos', {
        method: 'POST',
        identidad: 'admin',
        body: JSON.stringify(datos),
      });
    },

    async actualizarEvento(eventoId, datos) {
      if (estado.modoDemo) {
        const evento = estado.eventos.find((e) => e.evento_id === eventoId);
        if (evento) Object.assign(evento, datos);
        guardarLocal();
        return evento;
      }
      return pedir('/api/v1/butacas/eventos/' + encodeURIComponent(eventoId), {
        method: 'PUT',
        identidad: 'admin',
        body: JSON.stringify(datos),
      });
    },

    async eliminarEvento(eventoId) {
      if (estado.modoDemo) {
        estado.eventos = estado.eventos.filter((e) => e.evento_id !== eventoId);
        estado.secciones = estado.secciones.filter((s) => s.evento_id !== eventoId);
        guardarLocal();
        return null;
      }
      return pedir('/api/v1/butacas/eventos/' + encodeURIComponent(eventoId), {
        method: 'DELETE',
        identidad: 'admin',
      });
    },

    async crearSeccion(datos) {
      if (estado.modoDemo) {
        const seccion = Object.assign({ seccion_id: idUnico() }, datos);
        estado.secciones.push(seccion);
        estado.butacasPorSeccion[seccion.seccion_id] = generarButacasDemo(seccion);
        guardarLocal();
        return seccion;
      }
      return pedir('/api/v1/butacas/secciones', {
        method: 'POST',
        identidad: 'admin',
        body: JSON.stringify(datos),
      });
    },

    async actualizarSeccion(seccionId, datos) {
      if (estado.modoDemo) {
        const seccion = estado.secciones.find((s) => s.seccion_id === seccionId);
        if (seccion) Object.assign(seccion, datos);
        guardarLocal();
        return seccion;
      }
      return pedir('/api/v1/butacas/secciones/' + encodeURIComponent(seccionId), {
        method: 'PUT',
        identidad: 'admin',
        body: JSON.stringify(datos),
      });
    },

    async eliminarSeccion(seccionId) {
      if (estado.modoDemo) {
        estado.secciones = estado.secciones.filter((s) => s.seccion_id !== seccionId);
        guardarLocal();
        return null;
      }
      return pedir('/api/v1/butacas/secciones/' + encodeURIComponent(seccionId), {
        method: 'DELETE',
        identidad: 'admin',
      });
    },
  };

  function generarButacasDemo(seccion) {
    const filas = ['A', 'B', 'C', 'D', 'E', 'F'];
    const butacas = [];
    let numero = 1;
    for (const fila of filas) {
      for (let i = 0; i < 8; i += 1) {
        if (numero > seccion.capacidad_total) break;
        butacas.push({
          butaca_id: `${seccion.seccion_id}-${fila}${numero}`,
          seccion_id: seccion.seccion_id,
          fila,
          numero,
          estado: 'LIBRE',
        });
        numero += 1;
      }
    }
    return butacas;
  }

  /* ------------------------------------------------------------- programa */
  function renderPrograma() {
    const lista = $('#programa');
    lista.textContent = '';

    if (!estado.eventos.length) {
      lista.appendChild(crear('li', 'programa__nota', 'No hay eventos cargados.'));
      return;
    }

    for (const evento of estado.eventos) {
      const item = crear('li');
      const boton = crear('button', 'programa__item');
      boton.type = 'button';
      boton.setAttribute('aria-current', String(evento.evento_id === estado.eventoId));

      const fecha = crear('span', 'programa__fecha');
      const corto = fechaCorta(evento.fecha);
      fecha.appendChild(crear('span', 'programa__mes', corto.mes));
      fecha.appendChild(document.createTextNode(corto.dia));
      boton.appendChild(fecha);

      const cuerpo = crear('span');
      cuerpo.appendChild(crear('span', 'programa__nombre', evento.nombre));
      const seccionesDelEvento = estado.secciones.filter((s) => s.evento_id === evento.evento_id);
      const disponibles = seccionesDelEvento.reduce((total, s) => total + (s.cantidad_disponible ?? s.capacidad_total), 0);
      cuerpo.appendChild(crear('span', 'programa__meta', `${evento.lugar} · ${disponibles} butacas`));
      boton.appendChild(cuerpo);

      boton.addEventListener('click', () => seleccionarEvento(evento.evento_id));
      item.appendChild(boton);
      lista.appendChild(item);
    }
  }

  /* ----------------------------------------------------------------- sala */
  function seccionActual() {
    return estado.secciones.find((s) => s.seccion_id === estado.seccionId) || null;
  }

  function eventoActual() {
    return estado.eventos.find((e) => e.evento_id === estado.eventoId) || null;
  }

  async function seleccionarEvento(eventoId) {
    estado.eventoId = eventoId;
    estado.seleccion.clear();
    const secciones = estado.secciones.filter((s) => s.evento_id === eventoId);
    estado.seccionId = secciones.length ? secciones[0].seccion_id : null;
    renderPrograma();
    renderSala();
    if (estado.seccionId) await cargarButacas();
  }

  async function cargarButacas() {
    if (!estado.seccionId) {
      estado.butacas = [];
      renderPlano();
      return;
    }
    try {
      estado.butacas = await repo.butacas(estado.seccionId);
    } catch (error) {
      estado.butacas = [];
      aviso('No se pudo cargar el plano de la sala: ' + error.message, 'error');
    }
    renderPlano();
  }

  function renderSala() {
    const evento = eventoActual();
    const seccion = seccionActual();
    const titulo = $('#sala-titulo');
    const datos = $('#sala-datos');

    if (!evento) {
      titulo.textContent = 'Elige tus butacas';
      datos.textContent = '';
      $('#carrito').textContent = '';
      renderPlano();
      return;
    }

    titulo.textContent = evento.nombre;
    datos.textContent = '';

    const fecha = crear('span');
    fecha.appendChild(document.createTextNode('Fecha '));
    fecha.appendChild(crear('b', null, fechaLarga(evento.fecha) + ' ' + hora(evento.fecha)));
    datos.appendChild(fecha);

    const sala = crear('span');
    sala.appendChild(document.createTextNode('Recinto '));
    sala.appendChild(crear('b', null, evento.lugar));
    datos.appendChild(sala);

    if (seccion) {
      const seccionInfo = crear('span');
      seccionInfo.appendChild(document.createTextNode('Sección '));
      seccionInfo.appendChild(crear('b', null, seccion.nombre));
      seccionInfo.appendChild(document.createTextNode(' ' + monto(seccion.precio_centavos) + ' por entrada'));
      datos.appendChild(seccionInfo);
    }

    if (seccion && seccion.precio_centavos) {
      const selector = crear('select', 'campo__control');
      selector.id = 'selector-seccion';
      selector.style.maxWidth = '16rem';
      for (const opcion of estado.secciones.filter((s) => s.evento_id === estado.eventoId)) {
        const nodo = crear('option', null, `${opcion.nombre} · ${monto(opcion.precio_centavos)}`);
        nodo.value = opcion.seccion_id;
        if (opcion.seccion_id === estado.seccionId) nodo.selected = true;
        selector.appendChild(nodo);
      }
      selector.addEventListener('change', async (eventoCambio) => {
        estado.seccionId = eventoCambio.target.value;
        estado.seleccion.clear();
        renderSala();
        await cargarButacas();
      });
      datos.appendChild(selector);
    }

    renderPlano();
    renderCarrito();
  }

  function renderPlano() {
    const plano = $('#plano');
    plano.textContent = '';

    if (!estado.butacas.length) {
      plano.appendChild(crear('p', 'carrito__vacio', 'Sin plano disponible.'));
      return;
    }

    plano.appendChild(crear('div', 'plano__escenario', 'Escenario'));

    const filas = new Map();
    for (const butaca of estado.butacas) {
      if (!filas.has(butaca.fila)) filas.set(butaca.fila, []);
      filas.get(butaca.fila).push(butaca);
    }

    const orden = Array.from(filas.keys()).sort((a, b) => a.localeCompare(b, 'es', { numeric: true }));
    for (const fila of orden) {
      const contenedor = crear('div', 'fila');
      contenedor.appendChild(crear('span', 'fila__etiqueta', fila));

      const butacas = crear('div', 'fila__butacas');
      let indice = 0;
      for (const butaca of filas.get(fila)) {
        if (indice > 0 && indice % 8 === 0) {
          butacas.appendChild(crear('span', 'fila__pasillo'));
        }
        indice += 1;

        const ocupada = butaca.estado !== 'LIBRE';
        const boton = crear('button', 'butaca');
        boton.type = 'button';
        boton.disabled = ocupada;
        boton.setAttribute('aria-pressed', String(estado.seleccion.has(butaca.butaca_id)));
        boton.setAttribute(
          'aria-label',
          `Fila ${butaca.fila}, butaca ${butaca.numero}, ${ocupada ? 'ocupada' : estado.seleccion.has(butaca.butaca_id) ? 'seleccionada' : 'libre'}`
        );
        boton.title = `Fila ${butaca.fila}, butaca ${butaca.numero}`;
        boton.addEventListener('click', () => alternarButaca(butaca));
        butacas.appendChild(boton);
      }
      contenedor.appendChild(butacas);
      plano.appendChild(contenedor);
    }
  }

  function alternarButaca(butaca) {
    if (estado.seleccion.has(butaca.butaca_id)) {
      estado.seleccion.delete(butaca.butaca_id);
    } else {
      estado.seleccion.add(butaca.butaca_id);
    }
    renderPlano();
    renderCarrito();
  }

  function butacasSeleccionadas() {
    return estado.butacas.filter((b) => estado.seleccion.has(b.butaca_id));
  }

  function renderCarrito() {
    const carrito = $('#carrito');
    carrito.textContent = '';

    const seccion = seccionActual();
    const seleccion = butacasSeleccionadas();
    const lista = crear('div', 'carrito__lista');

    if (!seleccion.length) {
      lista.appendChild(crear('p', 'carrito__vacio', 'Selecciona tus butacas en el plano.'));
    } else {
      const resumen = crear('p');
      const total = seleccion.length * (seccion ? seccion.precio_centavos : 0);
      resumen.appendChild(
        crear('strong', null, `${seleccion.length} ${seleccion.length === 1 ? 'entrada' : 'entradas'}`)
      );
      resumen.appendChild(document.createTextNode(` · ${seccion ? seccion.nombre : ''}`));
      lista.appendChild(resumen);

      const pastillas = crear('div', 'carrito__butacas');
      for (const butaca of seleccion) {
        const pastilla = crear('span', 'pastilla', `${butaca.fila}${butaca.numero}`);
        pastilla.title = 'Quitar de la selección';
        pastilla.style.cursor = 'pointer';
        pastilla.addEventListener('click', () => alternarButaca(butaca));
        pastillas.appendChild(pastilla);
      }
      lista.appendChild(pastillas);
    }

    carrito.appendChild(lista);

    const total = crear('div', 'carrito__total');
    const valor = seleccion.length * (seccion ? seccion.precio_centavos : 0);
    total.appendChild(crear('span', 'carrito__monto', monto(valor)));
    total.appendChild(crear('span', 'carrito__unida', 'total a pagar'));
    carrito.appendChild(total);

    const boton = crear('button', 'boton', seleccion.length ? `Comprar ${seleccion.length}` : 'Comprar');
    boton.type = 'button';
    boton.disabled = !seleccion.length;
    boton.addEventListener('click', comprar);
    carrito.appendChild(boton);
  }

  async function comprar() {
    const seleccion = butacasSeleccionadas();
    const seccion = seccionActual();
    if (!seleccion.length || !seccion) return;

    const boton = document.querySelector('#carrito .boton');
    if (boton) boton.disabled = true;

    const sagaId = idUnico();
    const butacasElegidas = seleccion.map((b) => b.butaca_id);
    try {
      const resultado = await repo.comprar({
        saga_id: sagaId,
        evento_id: estado.eventoId,
        seccion_id: seccion.seccion_id,
        butaca_ids: butacasElegidas,
      });

      const compra = resultado.compra || {};
      const estadoSaga = compra.estado || 'CONFIRMADA';
      estado.entradas.unshift({
        saga_id: sagaId,
        estado: estadoSaga,
        simulado: Boolean(resultado.simulado),
        evento: eventoActual(),
        seccion: seccion,
        butacas: seleccion.map((b) => ({ butaca_id: b.butaca_id, fila: b.fila, numero: b.numero })),
        entradas: resultado.entradas || [],
        monto:
          compra.monto_total_centavos ||
          resultado.monto_total_centavos ||
          seleccion.length * seccion.precio_centavos,
        moneda: compra.moneda || resultado.moneda || 'CLP',
      });
      guardarLocal();
      const mensaje = mensajeDeCompra(resultado, estadoSaga);
      aviso(mensaje.texto, mensaje.nivel);
      estado.seleccion.clear();
      renderPlano();
      renderCarrito();
      cambiarVista('entradas');
    } catch (error) {
      aviso('No se pudo completar la compra: ' + error.message, 'error');
      if (boton) boton.disabled = false;
    }
  }

  /**
   * El aviso de la compra depende de **como termino la saga**. Anunciar
   * "Entradas compradas" cuando la saga quedo compensada (el pago se
   * revirtio y no se emitieron entradas) seria mentirle a la persona.
   *
   * @param {object} resultado lo que devolvio el repositorio
   * @param {string} estadoSaga estado final de la saga
   * @returns {{texto: string, nivel: string}}
   */
  function mensajeDeCompra(resultado, estadoSaga) {
    if (resultado.simulado) {
      return {
        texto:
          'Simulación local: el backend no está conectado, así que no se ejecutó ninguna Saga.',
        nivel: 'info',
      };
    }
    if (estadoSaga === 'CONFIRMADA') {
      return { texto: 'Entradas compradas.', nivel: 'info' };
    }
    if (estadoSaga === 'COMPENSADA') {
      return {
        texto:
          'La compra no se pudo completar: se revirtió el pago y no se emitieron entradas.',
        nivel: 'error',
      };
    }
    const detalle = String((resultado.compra && resultado.compra.error) || '').trim();
    return {
      texto: detalle
        ? 'No se pudo completar la compra: ' + detalle
        : 'No se pudo completar la compra.',
      nivel: 'error',
    };
  }

  /* -------------------------------------------------------------- stub */
  function matrizQr(texto, lado) {
    const celdas = [];
    let h = 2166136261;
    for (let i = 0; i < texto.length; i += 1) {
      h ^= texto.charCodeAt(i);
      h = Math.imul(h, 16777619);
    }
    for (let i = 0; i < lado * lado; i += 1) {
      h ^= h << 13;
      h ^= h >>> 17;
      h ^= h << 5;
      h |= 0;
      celdas.push((h & 1) === 1 ? 1 : 0);
    }

    // esquinas de lectura, como en un QR real
    const esquinas = [
      [0, 0],
      [0, lado - 7],
      [lado - 7, 0],
    ];
    for (const [fila, columna] of esquinas) {
      for (let f = 0; f < 7; f += 1) {
        for (let c = 0; c < 7; c += 1) {
          const borde = f === 0 || f === 6 || c === 0 || c === 6;
          const centro = f >= 2 && f <= 4 && c >= 2 && c <= 4;
          celdas[(fila + f) * lado + (columna + c)] = borde || centro ? 1 : 0;
        }
      }
    }
    return celdas;
  }

  function renderStub(compra) {
    const stub = crear('div', 'stub');

    const cuerpo = crear('div', 'stub__cuerpo');
    cuerpo.appendChild(crear('p', 'stub__evento', compra.evento ? compra.evento.nombre : 'Evento'));
    cuerpo.appendChild(
      crear('p', 'stub__datos', `${compra.seccion ? compra.seccion.nombre : ''} · ${compra.evento ? fechaLarga(compra.evento.fecha) : ''}`)
    );

    const filas = crear('div', 'stub__fila');
    const butacas = compra.butacas.map((b) => `${b.fila}${b.numero}`).join(' ');
    const primera = crear('span');
    primera.appendChild(document.createTextNode('Butacas '));
    primera.appendChild(crear('b', null, butacas));
    filas.appendChild(primera);

    const segunda = crear('span');
    segunda.appendChild(document.createTextNode('Total '));
    segunda.appendChild(crear('b', null, monto(compra.monto)));
    filas.appendChild(segunda);
    cuerpo.appendChild(filas);

    const saga = crear('span', 'stub__datos', `Saga ${compra.saga_id.slice(0, 12)}`);
    saga.title = compra.saga_id;
    cuerpo.appendChild(saga);

    stub.appendChild(cuerpo);

    const qrBloque = crear('div', 'stub__qr');
    const entrada = (compra.entradas && compra.entradas[0]) || {};
    const contenido = entrada.qr || compra.saga_id;
    const qr = crear('div', 'qr');
    const celdas = matrizQr(String(contenido), 21);
    for (const llena of celdas) {
      const celda = crear('span');
      celda.dataset.lleno = String(llena);
      qr.appendChild(celda);
    }
    qr.setAttribute('role', 'img');
    qr.setAttribute('aria-label', 'Código de la entrada');
    qrBloque.appendChild(qr);

    const codigo = crear('p', 'stub__codigo', (entrada.entrada_id || compra.saga_id).slice(0, 12).toUpperCase());
    qrBloque.appendChild(codigo);

    const estadoStub = crear('p', 'stub__estado', compra.estado || 'CONFIRMADA');
    estadoStub.dataset.estado = compra.estado || 'CONFIRMADA';
    qrBloque.appendChild(estadoStub);

    if (compra.simulado) {
      const marca = crear('p', 'stub__estado', 'Simulación local');
      marca.dataset.estado = 'SIMULADA';
      qrBloque.appendChild(marca);
    }

    stub.appendChild(qrBloque);
    return stub;
  }

  function renderEntradas() {
    const contenedor = $('#entradas');
    contenedor.textContent = '';

    if (!estado.entradas.length) {
      contenedor.appendChild(
        crear('p', 'entradas__vacio', 'Todavía no tienes entradas. Elige un evento y compra tus butacas.')
      );
      return;
    }

    for (const compra of estado.entradas) {
      const bloque = crear('div');
      bloque.appendChild(renderStub(compra));
      contenedor.appendChild(bloque);
    }
  }

  /* --------------------------------------------------------------- admin */
  function renderAdmin() {
    const cuerpo = $('#admin-eventos');
    cuerpo.textContent = '';

    for (const evento of estado.eventos) {
      const fila = crear('tr');
      fila.appendChild(crear('td', null, evento.nombre));
      fila.appendChild(crear('td', null, evento.lugar));
      fila.appendChild(crear('td', 'tabla__numero', fechaCorta(evento.fecha).dia + ' ' + fechaCorta(evento.fecha).mes));
      fila.appendChild(
        crear('td', 'tabla__numero', String(estado.secciones.filter((s) => s.evento_id === evento.evento_id).length))
      );

      const acciones = crear('td');
      const editar = crear('button', 'boton boton--fantasma boton--chico', 'Editar');
      editar.type = 'button';
      editar.addEventListener('click', () => editarEvento(evento));
      acciones.appendChild(editar);

      const secciones = crear('button', 'boton boton--fantasma boton--chico', 'Secciones');
      secciones.type = 'button';
      secciones.addEventListener('click', () => {
        estado.eventoDeSecciones = evento.evento_id;
        renderSecciones();
      });
      acciones.appendChild(secciones);

      const borrar = crear('button', 'boton boton--peligro boton--chico', 'Eliminar');
      borrar.type = 'button';
      borrar.addEventListener('click', async () => {
        if (!window.confirm('¿Eliminar "' + evento.nombre + '"?')) return;
        await repo.eliminarEvento(evento.evento_id);
        // Igual que al guardar: se recarga el catálogo para que la fila
        // desaparezca de verdad y no quede una foto del estado anterior.
        await repo.catalogo();
        renderAdmin();
        renderPrograma();
      });
      acciones.appendChild(borrar);

      fila.appendChild(acciones);
      cuerpo.appendChild(fila);
    }

    renderSecciones();
  }

  function renderSecciones() {
    const cuerpo = $('#admin-secciones-tbody');
    if (!cuerpo) return;
    cuerpo.textContent = '';

    const evento = estado.eventos.find((e) => e.evento_id === estado.eventoDeSecciones);
    $('#secciones-evento').textContent = evento ? evento.nombre : '—';

    const secciones = evento ? estado.secciones.filter((s) => s.evento_id === evento.evento_id) : [];
    for (const seccion of secciones) {
      const fila = crear('tr');
      fila.appendChild(crear('td', null, seccion.nombre));
      fila.appendChild(crear('td', 'tabla__numero', String(seccion.capacidad_total)));
      fila.appendChild(crear('td', 'tabla__numero', monto(seccion.precio_centavos)));
      fila.appendChild(crear('td', 'tabla__numero', String(seccion.cantidad_disponible ?? seccion.capacidad_total)));

      const acciones = crear('td');
      const editar = crear('button', 'boton boton--fantasma boton--chico', 'Editar');
      editar.type = 'button';
      editar.addEventListener('click', () => editarSeccion(seccion));
      acciones.appendChild(editar);

      const borrar = crear('button', 'boton boton--peligro boton--chico', 'Eliminar');
      borrar.type = 'button';
      borrar.addEventListener('click', async () => {
        if (!window.confirm('¿Eliminar la sección "' + seccion.nombre + '"?')) return;
        await repo.eliminarSeccion(seccion.seccion_id);
        await repo.catalogo();
        renderSecciones();
        renderPrograma();
      });
      acciones.appendChild(borrar);

      fila.appendChild(acciones);
      cuerpo.appendChild(fila);
    }
  }

  function editarEvento(evento) {
    estado.editandoEvento = evento ? evento.evento_id : null;
    const formulario = $('#formulario-evento');
    // El formulario arranca oculto (como el de secciones): el botón que lo
    // abre es el que decide si se crea un evento o se edita uno existente.
    formulario.hidden = false;
    $('#formulario-titulo').textContent = evento ? 'Editar evento' : 'Nuevo evento';
    formulario.nombre.value = evento ? evento.nombre : '';
    formulario.lugar.value = evento ? evento.lugar : '';
    formulario.fecha.value = evento ? (evento.fecha || '').slice(0, 16) : '';
    $('#formulario-error').hidden = true;
  }

  function editarSeccion(seccion) {
    estado.editandoSeccion = seccion ? seccion.seccion_id : null;
    const formulario = $('#formulario-seccion');
    formulario.hidden = false;
    $('#formulario-seccion-titulo').textContent = seccion ? 'Editar sección' : 'Nueva sección';
    formulario.nombre.value = seccion ? seccion.nombre : '';
    formulario.capacidad_total.value = seccion ? seccion.capacidad_total : '';
    formulario.precio.value = seccion ? Math.round(seccion.precio_centavos / 100) : '';
    $('#formulario-seccion-error').hidden = true;
  }

  async function guardarEvento(eventoEnvio) {
    const error = $('#formulario-error');
    error.hidden = true;

    const datos = {
      nombre: eventoEnvio.nombre.value.trim(),
      lugar: eventoEnvio.lugar.value.trim(),
      fecha: eventoEnvio.fecha.value,
    };

    if (!datos.nombre || !datos.lugar || !datos.fecha) {
      error.textContent = 'Completa nombre, sala y fecha.';
      error.hidden = false;
      return;
    }

    try {
      if (estado.editandoEvento) {
        await repo.actualizarEvento(estado.editandoEvento, datos);
      } else {
        await repo.crearEvento(datos);
      }
      editarEvento(null);
      $('#formulario-evento').hidden = true;
      // Recarga el catálogo: sin esto la tabla y el programa seguirían
      // mostrando la lista anterior y el guardado no se vería en pantalla.
      await repo.catalogo();
      renderAdmin();
      renderPrograma();
      aviso('Evento guardado.', 'info');
    } catch (fallo) {
      error.textContent = 'No se pudo guardar: ' + fallo.message;
      error.hidden = false;
    }
  }

  async function guardarSeccion(eventoEnvio) {
    const error = $('#formulario-seccion-error');
    error.hidden = true;

    if (!estado.eventoDeSecciones) {
      error.textContent = 'Elige primero un evento en la tabla de arriba.';
      error.hidden = false;
      return;
    }

    const datos = {
      evento_id: estado.eventoDeSecciones,
      nombre: eventoEnvio.nombre.value.trim(),
      capacidad_total: Number(eventoEnvio.capacidad_total.value),
      precio_centavos: Math.round(Number(eventoEnvio.precio.value) * 100),
    };

    if (!datos.nombre || !datos.capacidad_total || datos.precio_centavos < 0) {
      error.textContent = 'Revisa nombre, capacidad y precio.';
      error.hidden = false;
      return;
    }

    try {
      if (estado.editandoSeccion) {
        await repo.actualizarSeccion(estado.editandoSeccion, datos);
      } else {
        await repo.crearSeccion(datos);
      }
      editarSeccion(null);
      $('#formulario-seccion').hidden = true;
      await repo.catalogo();
      renderSecciones();
      renderPrograma();
      aviso('Sección guardada.', 'info');
    } catch (fallo) {
      error.textContent = 'No se pudo guardar: ' + fallo.message;
      error.hidden = false;
    }
  }

  /* ------------------------------------------------------------ navegación */
  // La barra muestra con qué identidad se está trabajando: el correo con el
  // que el gateway abrió la sesión.
  function mostrarSesion() {
    const nodo = $('#sesion');
    const boton = $('#salir');
    if (!nodo || !boton) return;
    if (estado.token) {
      nodo.textContent = estado.usuario ? estado.usuario.email : 'sesión iniciada';
      nodo.hidden = false;
      boton.hidden = false;
    } else {
      nodo.textContent = '';
      nodo.hidden = true;
      boton.hidden = true;
    }
  }

  function cambiarVista(vista) {
    estado.vista = vista;
    $('#vista-comprar').hidden = vista !== 'comprar';
    $('#vista-entradas').hidden = vista !== 'entradas';
    $('#vista-admin').hidden = vista !== 'admin';

    for (const boton of document.querySelectorAll('.nav__boton')) {
      if (boton.dataset.vista === vista) boton.setAttribute('aria-current', 'page');
      else boton.removeAttribute('aria-current');
    }

    if (vista === 'entradas') renderEntradas();
    if (vista === 'admin') renderAdmin();
  }

  function conectar() {
    for (const boton of document.querySelectorAll('.nav__boton')) {
      boton.addEventListener('click', () => cambiarVista(boton.dataset.vista));
    }

    $('#nuevo-evento').addEventListener('click', () => editarEvento(null));
    $('#cancelar-evento').addEventListener('click', () => {
      editarEvento(null);
      $('#formulario-evento').hidden = true;
    });
    $('#formulario-evento').addEventListener('submit', (eventoEnvio) => {
      eventoEnvio.preventDefault();
      guardarEvento(eventoEnvio.target);
    });

    $('#nueva-seccion').addEventListener('click', () => editarSeccion(null));
    $('#cancelar-seccion').addEventListener('click', () => {
      editarSeccion(null);
      $('#formulario-seccion').hidden = true;
    });
    $('#formulario-seccion').addEventListener('submit', (eventoEnvio) => {
      eventoEnvio.preventDefault();
      guardarSeccion(eventoEnvio.target);
    });

    $('#salir').addEventListener('click', () => {
      // Cerrar la sesión olvida los dos tokens: el siguiente pedido vuelve a
      // pedir credenciales al gateway.
      estado.token = null;
      estado.tokenAdmin = null;
      estado.usuario = null;
      estado.esAdmin = false;
      expiraciones.cliente = 0;
      expiraciones.admin = 0;
      mostrarSesion();
      aviso('Sesión cerrada. El siguiente pedido volverá a pedir credenciales.', 'info');
    });
  }

  /* ----------------------------------------------------------------- inicio */
  async function iniciar() {
    conectar();
    await repo.catalogo();

    if (estado.modoDemo) {
      aviso(AVISOS_DEMO[estado.motivoDemo] || AVISOS_DEMO['sin-respuesta'], 'info');
    } else if (estado.pagosDisponible === false) {
      aviso('El servicio de pagos está degradado: se puede ver el catálogo, pero no comprar.', 'error');
    }

    mostrarSesion();

    if (estado.eventos.length) {
      await seleccionarEvento(estado.eventos[0].evento_id);
    } else {
      renderSala();
    }
  }

  // Sonda para las pruebas automatizadas del front (`node --test
  // frontend/web/tests/`): expone el estado y el repositorio para poder
  // comprobar el flujo de autenticación sin navegador. No cambia el
  // comportamiento de la interfaz.
  window.TICKETERA_APP = { estado, repo, pedir, iniciar };

  document.addEventListener('DOMContentLoaded', iniciar);
})();
