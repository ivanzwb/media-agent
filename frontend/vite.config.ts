import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

const BACKEND = "http://127.0.0.1:8000";

// Client-side (SPA) page routes that must render the React app even though
// their path prefix is also used by backend action endpoints.
const SPA_PAGE_RE = [
  /^\/sources(\?.*)?$/,
  /^\/drafts(\?.*)?$/,
  /^\/drafts\/\d+\/edit(\?.*)?$/,
  /^\/archive(\?.*)?$/,
  /^\/archive\/\d+\/view(\?.*)?$/,
  /^\/settings(\?.*)?$/,
];

function pageBypass(req: any): string | undefined {
  if (req.method === "GET" && SPA_PAGE_RE.some((re) => re.test(req.url || ""))) {
    return "/index.html"; // serve SPA instead of proxying
  }
  return undefined;
}

const proxy: Record<string, any> = {};
// Pure API / asset prefixes: always proxy.
for (const p of ["/api", "/run", "/clear", "/export", "/import",
  "/images", "/media", "/videos", "/voices-audio", "/static"]) {
  proxy[p] = { target: BACKEND, changeOrigin: true };
}
// Prefixes shared between SPA pages and backend actions: proxy, but bypass
// the SPA page GETs so client-side routing works in dev.
for (const p of ["/sources", "/drafts", "/archive", "/settings"]) {
  proxy[p] = { target: BACKEND, changeOrigin: true, bypass: pageBypass };
}

export default defineConfig({
  plugins: [react()],
  build: { outDir: "dist", emptyOutDir: true },
  server: { port: 5173, proxy },
});
