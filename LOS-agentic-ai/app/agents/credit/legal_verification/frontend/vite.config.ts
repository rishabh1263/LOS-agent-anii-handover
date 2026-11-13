/// <reference types="vitest" />
import { defineConfig, loadEnv } from "vite";
import react from "@vitejs/plugin-react";

// The API is the LOS server's (8010: /api/v1/credit/legal-verification); LGV_API overrides.
export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, ".", "LGV_");
  return {
    base: "./",
    plugins: [react()],
    server: { port: 5176, strictPort: true, proxy: { "/api": env.LGV_API || "http://127.0.0.1:8010" } },
    test: { environment: "jsdom", setupFiles: ["./src/test/setup.ts"], globals: true, testTimeout: 30000 },
  };
});
