/* Service worker de Pronósticos
   - Página y datos (index.html, web/*.json): se intenta la red durante 1,5 s; si tarda más o no hay
     conexión se muestra lo guardado al momento y la red sigue descargando por detrás. Si llega algo
     distinto de lo que se enseñó, se avisa a la página ("hay datos nuevos").
   - Iconos, manifest y tipografía: siempre desde lo guardado (no cambian).
   La caché de la app se renueva con cada versión; la de datos se conserva y se poda (días de más de 40 días). */
const VERSION = 'pronosticos-__VERSION__';
const APP = 'app-' + VERSION;
const DATOS = 'pron-datos';
const BASE = ['./', './manifest.webmanifest', './icono-192.png', './icono-512.png', './apple-touch-icon.png',
              './urbanist.woff2', './urbanist-italic.woff2'];
const ESPERA = 1500;

self.addEventListener('install', e => {
  e.waitUntil(caches.open(APP).then(c => c.addAll(BASE)).then(() => self.skipWaiting()));
});

self.addEventListener('activate', e => {
  e.waitUntil((async () => {
    for (const k of await caches.keys()) if (k !== APP && k !== DATOS) await caches.delete(k);
    // podar días viejos de la caché de datos
    const c = await caches.open(DATOS), limite = new Date(Date.now() - 40 * 864e5).toISOString().slice(0, 10);
    for (const req of await c.keys()) {
      const m = req.url.match(/dias\/(\d{4}-\d{2}-\d{2})\.json/);
      if (m && m[1] < limite) await c.delete(req);
    }
    await self.clients.claim();
  })());
});

async function avisar(url) {
  for (const cl of await self.clients.matchAll()) cl.postMessage({tipo: 'nuevo', url});
}

function redPrimeroConPlazo(e, cacheNombre, clave) {
  const cache = caches.open(cacheNombre);
  const guardada = cache.then(c => c.match(clave, {ignoreSearch: true}));
  const red = fetch(e.request).then(async r => {
    if (r.ok) {
      const c = await cache, antes = await guardada;
      await c.put(clave, r.clone());
      const cambia = antes && (r.headers.get('etag') || '') !== (antes.headers.get('etag') || '');
      return {r, cambia};
    }
    return {r, cambia: false};
  });
  return new Promise(resolve => {
    let hecho = false;
    const fin = x => { if (!hecho && x) { hecho = true; resolve(x); } };
    const plazo = setTimeout(async () => fin(await guardada), ESPERA);
    red.then(({r}) => { clearTimeout(plazo); fin(r); })
       .catch(async () => { clearTimeout(plazo); fin((await guardada) || await caches.match('./', {ignoreSearch: true}) || Response.error()); });
    // si ya se enseñó lo guardado y la red trae algo nuevo, avisar a la página
    e.waitUntil(red.then(({cambia}) => { if (hecho && cambia) return avisar(clave); }).catch(() => {}));
  });
}

self.addEventListener('fetch', e => {
  const url = new URL(e.request.url);
  if (e.request.method !== 'GET' || url.origin !== location.origin) return;
  const ruta = url.pathname;
  if (e.request.mode === 'navigate' || ruta.endsWith('/') || ruta.endsWith('/index.html')) {
    e.respondWith(redPrimeroConPlazo(e, APP, './'));
  } else if (/\/web\/.*\.json$/.test(ruta)) {
    e.respondWith(redPrimeroConPlazo(e, DATOS, url.origin + ruta));
  } else {
    e.respondWith(caches.match(e.request, {ignoreSearch: true}).then(r => r || fetch(e.request)));
  }
});
