/**
 * Copyright 2026 Google LLC
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     https://www.apache.org/licenses/LICENSE-2.0
 *
 * Unless required by applicable law or agreed to in writing, software
 * distributed under the License is distributed on an "AS IS" BASIS,
 * WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 * See the License for the specific language governing permissions and
 * limitations under the License.
 */

/// <reference types="vitest/config" />
import { fileURLToPath } from "node:url";
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Generation runs stream for up to 15 minutes. VITE_API_TARGET points the dev server at another backend.
const backend = {
  target: process.env.VITE_API_TARGET ?? "http://localhost:8000",
  changeOrigin: true,
  timeout: 900_000,
  proxyTimeout: 900_000,
};

export default defineConfig({
  plugins: [react()],
  publicDir: false,
  resolve: {
    alias: { "@": fileURLToPath(new URL("./src", import.meta.url)) },
  },
  build: {
    rollupOptions: {
      output: {
        // Vite 8 / Rolldown requires a function (object form was removed).
        manualChunks: (id) => {
          if (!id.includes("node_modules")) return;
          if (
            id.includes("/react-router-dom/") ||
            id.includes("/react-dom/") ||
            id.includes("/react/")
          ) {
            return "vendor";
          }
        },
      },
    },
  },
  server: {
    port: 3000,
    open: true,
    proxy: { "/api": backend, "/health": backend },
  },
  test: {
    environment: "jsdom",
    setupFiles: ["./src/test/setup.ts"],
    restoreMocks: true,
    unstubGlobals: true,
  },
});
