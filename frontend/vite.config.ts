/// <reference types="vitest" />
import { fileURLToPath, URL } from "node:url";
import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";

// API routes live at the server root; the console is served under /console/.
const API = process.env.VITE_API_TARGET ?? "http://127.0.0.1:8000";
const apiPaths = ["/auth", "/me", "/sessions", "/runs", "/health", "/ready", "/admin"];

export default defineConfig({
  base: "/console/",
  plugins: [react(), tailwindcss()],
  resolve: { alias: { "@": fileURLToPath(new URL("./src", import.meta.url)) } },
  server: {
    port: 5173,
    proxy: Object.fromEntries(apiPaths.map((p) => [p, { target: API, changeOrigin: true }])),
  },
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: ["./src/test-setup.ts"],
  },
});
