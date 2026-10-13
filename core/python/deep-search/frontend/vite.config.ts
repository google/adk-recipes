// Copyright 2026 Google LLC
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     https://www.apache.org/licenses/LICENSE-2.0
//
// Unless required by applicable law or agreed to in writing, software
// distributed under the License is distributed on an "AS IS" BASIS,
// WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
// See the License for the specific language governing permissions and
// limitations under the License.

import path from "node:path";
import tailwindcss from "@tailwindcss/vite";
import react from "@vitejs/plugin-react-swc";
import { defineConfig } from "vite";

// https://vitejs.dev/config/
export default defineConfig({
  plugins: [react(), tailwindcss()],
  base: "/",
  resolve: {
    alias: {
      "@": path.resolve(new URL(".", import.meta.url).pathname, "./src"),
    },
  },
  server: {
    // Makes the server accessible on the local network (e.g., for mobile testing)
    host: true,
    // Should be disabled or limited when deployed in untrusted network environments.
    allowedHosts: true,
    proxy: {
      // Proxy API requests to the backend server
      "/api": {
        target: "http://127.0.0.1:8000", // Default backend address
        changeOrigin: true,
        secure: false,
        rewrite: (path) => path.replace(/^\/api/, ""),
        configure: (proxy) => {
          proxy.on("error", (err) => {
            console.error("proxy error", err);
          });
          proxy.on("proxyReq", (_proxyReq, req) => {
            console.debug(
              "Sending Request to the Target:",
              req.method,
              req.url,
            );
          });
          proxy.on("proxyRes", (proxyRes, req) => {
            console.debug(
              "Received Response from the Target:",
              proxyRes.statusCode,
              req.url,
            );
          });
        },
      },
    },
  },
});
