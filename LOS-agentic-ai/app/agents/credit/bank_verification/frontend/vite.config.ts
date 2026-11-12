/// <reference types="vitest" />
import { defineConfig, loadEnv } from "vite";
import react from "@vitejs/plugin-react";

// The API runs on 8041 (python -m uvicorn app.agents.credit.bank_verification.main:app --port 8041); BV_API overrides.
export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, ".", "BV_");
  return {
    // relative assets: the same build is served at / (8041) and at /bank-verification/ (the LOS server, 8010)
    base: "./",
    plugins: [react()],
    server: { port: 5174, strictPort: true, proxy: { "/api": env.BV_API || "http://127.0.0.1:8041" } },
    test: { environment: "jsdom", setupFiles: ["./src/test/setup.ts"], globals: true },
  };
});
