/// <reference types="vitest/config" />
import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// Ports are env-driven so a clone with a port collision can move them from
// KB_Core/.env, the same way every other service does. kb-svc.sh exports these
// two when it starts the `frontend` service. Defaults reproduce the historical
// values (preview 8091; dashboard API 8000). A bad/empty value falls back.
const PREVIEW_PORT = Number(process.env.KB_FRONTEND_PORT) || 8091;
const API_TARGET = `http://127.0.0.1:${Number(process.env.KB_DASHBOARD_PORT) || 8000}`;

export default defineConfig({
  plugins: [react()],
  // ws: true proxies the /api/ws WebSocket (the live change feed) alongside REST.
  server: { proxy: { "/api": { target: API_TARGET, ws: true } } },
  // `vite preview` serves the built dist/. It binds to loopback only and proxies
  // /api to the KB dashboard backend so the SPA stays same-origin. That proxy
  // forwards straight to the API, which serves every request as the owner
  // unless KB_DASHBOARD_AUTH=cf-access is set: never tunnel or port-forward this
  // port without that (see the README, "Exposing the dashboard").
  preview: {
    host: "127.0.0.1",
    port: PREVIEW_PORT,
    strictPort: true,
    // A tunnel or reverse proxy forwards your public Host header; Vite blocks unknown hosts
    // (anti-DNS-rebinding) unless allow-listed. Loopback IPs are always allowed.
    // Set KB_FRONTEND_ALLOWED_HOSTS to a comma-separated hostname list.
    allowedHosts: (process.env.KB_FRONTEND_ALLOWED_HOSTS ?? "")
      .split(",")
      .map((h) => h.trim())
      .filter(Boolean),
    proxy: {
      "/api": { target: API_TARGET, changeOrigin: true, ws: true },
    },
  },
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: "./src/test/setup.ts",
  },
});
