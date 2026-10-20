/* Service worker de CrossSpice Web: guarda la app completa (Pyodide, ngspice,
   núcleo y librería básica) para que funcione sin conexión.
   build.py actualiza VERSION y precache.json; no editar VERSION a mano. */
const VERSION = "428f6d51fabf";
const CACHE = "crossspice-" + VERSION;
// En la PC de desarrollo se prefiere la red para ver los cambios al instante.
const DEV = ["localhost", "127.0.0.1"].includes(self.location.hostname);

self.addEventListener("install", (e) => {
  e.waitUntil((async () => {
    const list = await (await fetch("precache.json", { cache: "no-store" })).json();
    const cache = await caches.open(CACHE);
    // De a pocos para no saturar la conexión de la tableta
    const files = list.files;
    for (let i = 0; i < files.length; i += 6) {
      await cache.addAll(files.slice(i, i + 6));
    }
    await self.skipWaiting();
  })());
});

self.addEventListener("activate", (e) => {
  e.waitUntil((async () => {
    const keys = await caches.keys();
    await Promise.all(keys.filter((k) => k.startsWith("crossspice-") && k !== CACHE)
      .map((k) => caches.delete(k)));
    await self.clients.claim();
  })());
});

self.addEventListener("fetch", (e) => {
  const req = e.request;
  if (req.method !== "GET") return;
  const url = new URL(req.url);
  if (url.origin !== location.origin) return;
  const scope = new URL(self.registration.scope);
  if (!url.pathname.startsWith(scope.pathname)) return;   // p. ej. /ltspice/ en pruebas
  if (DEV) {
    e.respondWith(fetch(req).then((res) => {
      if (res.ok) { const c = res.clone(); caches.open(CACHE).then((k) => k.put(req, c)); }
      return res;
    }).catch(() => caches.match(req, { ignoreSearch: true })));
    return;
  }
  e.respondWith(caches.match(req, { ignoreSearch: true }).then((hit) => hit || fetch(req)));
});
