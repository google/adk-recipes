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

import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

const BACKEND_URL = process.env.VITE_BACKEND_URL || "http://127.0.0.1:8000";

export default defineConfig({
  plugins: [react()],
  server: {
    port: 3000,
    host: "127.0.0.1",
    proxy: {
      "/api": {
        target: BACKEND_URL,
        changeOrigin: true,
        headers: {
          Origin: BACKEND_URL,
        },
      },
      "/apps": {
        target: BACKEND_URL,
        changeOrigin: true,
        headers: {
          Origin: BACKEND_URL,
        },
      },
      "/list-apps": {
        target: BACKEND_URL,
        changeOrigin: true,
        headers: {
          Origin: BACKEND_URL,
        },
      },
      "/run_live": {
        target: BACKEND_URL.replace(/^http/, "ws"),
        ws: true,
        changeOrigin: true,
        headers: {
          Origin: BACKEND_URL,
        },
      },
    },
  },
});
