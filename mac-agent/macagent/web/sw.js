// Minimal service worker: makes Relay installable. No caching of API data —
// the app is useless offline, and stale approvals would be dangerous.
self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", (e) => e.waitUntil(self.clients.claim()));
self.addEventListener("fetch", () => {});
