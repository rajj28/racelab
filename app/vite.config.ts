import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// The API writes to a real ledger and holds one CockroachDB connection per
// agent, so it stays on localhost. In dev the browser talks to Vite and Vite
// proxies /api through -- which keeps the front end origin-agnostic, so the
// built bundle works unchanged when Flask serves it from app/dist.
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      "/api": {
        target: "http://127.0.0.1:8000",
        changeOrigin: true,
        // Server-Sent Events must not be buffered by the proxy, or the run
        // arrives as one lump at the end and the whole point is lost.
        configure: (proxy) => {
          proxy.on("proxyRes", (res) => { res.headers["cache-control"] = "no-cache"; });
        },
      },
    },
  },
  build: { outDir: "dist", emptyOutDir: true },
});
