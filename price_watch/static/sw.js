// Service worker mínimo: cacheia só arquivos estáticos; páginas vêm sempre da rede
// (dados pessoais não ficam em cache).
const CACHE = "pw-static-v10";
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
        () => new Response("<meta name=viewport content='width=device-width'><p style='font-family:sans-serif;padding:2rem'>Sem conexão com o MeuPrecinho.</p>",
                           { headers: { "Content-Type": "text/html; charset=utf-8" } })
      )
    );
  }
});

// Notificações do app (Web Push): mostra o aviso e, ao tocar, abre a tela certa
self.addEventListener("push", (e) => {
  let data = {};
  try { data = e.data ? e.data.json() : {}; } catch (err) { data = { body: e.data && e.data.text() }; }
  e.waitUntil(
    self.registration.showNotification(data.title || "MeuPrecinho", {
      body: data.body || "",
      icon: "/static/icons/icon-192.png",
      badge: "/static/icons/badge-96.png",
      tag: data.tag || undefined,
      renotify: Boolean(data.tag),
      data: { url: data.url || "/alerts" },
    })
  );
});

self.addEventListener("notificationclick", (e) => {
  e.notification.close();
  const target = new URL((e.notification.data && e.notification.data.url) || "/alerts", self.location.origin).href;
  e.waitUntil(
    self.clients.matchAll({ type: "window", includeUncontrolled: true }).then((wins) => {
      const same = wins.find((w) => w.url.startsWith(self.location.origin));
      if (same && target.startsWith(self.location.origin)) {
        return same.navigate(target).then((w) => (w || same).focus());
      }
      return self.clients.openWindow(target);
    })
  );
});
