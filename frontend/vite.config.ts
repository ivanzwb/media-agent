import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Backend (FastAPI) endpoints the SPA calls. In dev, Vite proxies these to the
// running FastAPI server so we can use the Vite dev server with HMR.
const BACKEND = "http://127.0.0.1:8000";
// Only proxy non-page prefixes; SPA owns /, /archive, /drafts, /sources,
// /settings. All SPA-facing data/actions live under /api/* (or these asset
// prefixes) so there is no conflict with client-side routes.
const proxyPrefixes = [
  "/api", "/run", "/clear", "/export", "/import",
  "/images", "/media", "/videos", "/voices-audio", "/static",
];

export default defineConfig({
  plugins: [react()],
  build: {
    outDir: "dist",
    emptyOutDir: true,
  },
  server: {
    port: 5173,
    proxy: Object.fromEntries(
      proxyPrefixes.map((p) => [p, { target: BACKEND, changeOrigin: true }])
    ),
  },
});
