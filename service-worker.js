/* Risk Sizer uses no offline price cache: an old stop instruction is unsafe. */
self.addEventListener("install", function () { self.skipWaiting(); });
self.addEventListener("activate", function (event) { event.waitUntil(self.clients.claim()); });

self.addEventListener("push", function (event) {
  var payload = {};
  try { payload = event.data ? event.data.json() : {}; } catch (error) { payload = {}; }
  var title = payload.title || "Risk Sizer";
  var options = {
    body: payload.body || "Open Risk Sizer to review your stop.",
    icon: "/risk-sizer-icon.svg",
    badge: "/risk-sizer-icon.svg",
    tag: payload.tag || "risk-sizer-alert",
    renotify: true,
    data: { url: payload.url || "/" }
  };
  event.waitUntil(self.registration.showNotification(title, options));
});

self.addEventListener("notificationclick", function (event) {
  event.notification.close();
  var url = new URL((event.notification.data && event.notification.data.url) || "/", self.location.origin).href;
  event.waitUntil(self.clients.matchAll({ type: "window", includeUncontrolled: true }).then(function (clients) {
    for (var i = 0; i < clients.length; i++) {
      if (clients[i].url === url && "focus" in clients[i]) return clients[i].focus();
    }
    return self.clients.openWindow(url);
  }));
});
