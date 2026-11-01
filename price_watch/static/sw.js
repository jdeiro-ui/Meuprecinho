// Service worker mínimo: cacheia só arquivos estáticos; páginas vêm sempre da rede
// (dados pessoais não ficam em cache).
const CACHE = "pw-static-v2";
const ASSETS = ["/static/style.css", "/static/htmx.min.js", "/static/icons/icon-192.png"];

self.addEventListener("install", (e) => {
  e.waitUntil(caches.open(CACHE).then((c) => c.addAll(ASSETS)));
  self.skipWaiting();
});

self.addEventListener("activate", (e) => {
  e.waitUntil(
    caches.keys().then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
  );
  self.clients.claim();
});

self.addEventListener("fetch", (e) => {
  const url = new URL(e.request.url);
  if (e.request.method !== "GET" || url.origin !== location.origin) return;
  if (url.pathname.startsWith("/static/")) {
    e.respondWith(caches.match(e.request).then((hit) => hit || fetch(e.request)));
  } else if (e.request.mode === "navigate") {
    e.respondWith(
      fetch(e.request).catch(
        () => new Response("<meta name=viewport content='width=device-width'><p style='font-family:sans-serif;padding:2rem'>Sem conexão com o price-watch.</p>",
                           { headers: { "Content-Type": "text/html; charset=utf-8" } })
      )
    );
  }
});
