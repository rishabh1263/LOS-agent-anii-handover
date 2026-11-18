/// <reference types="vitest" />
import { defineConfig, loadEnv } from "vite";
import react from "@vitejs/plugin-react";

// The API is the LOS server's (8010: /api/v1/credit/crime-verification); CRV_API overrides.
export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, ".", "CRV_");
  return {
    base: "./",
    plugins: [react()],
    server: { port: 5175, strictPort: true, proxy: { "/api": env.CRV_API || "http://127.0.0.1:8010" } },
    test: { environment: "jsdom", setupFiles: ["./src/test/setup.ts"], globals: true },
  };
});
