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

// `npm run dev:mock`: the dev server with /api and /health answered by the msw
// fixture handlers (src/test/mockServer.ts) instead of the proxy to the backend.
import { Readable } from "node:stream";
import { getResponse } from "msw";
import { defineConfig, mergeConfig, type Plugin } from "vite";
import baseConfig from "./vite.config";
import { configureMock, handlers } from "./src/test/mockServer";

const ORIGIN = "http://localhost";

function mockBackend(): Plugin {
  configureMock({ eventDelayMs: 700 });
  return {
    name: "genmedia-mock-backend",
    // Middlewares added here run before Vite's proxy.
    configureServer(server) {
      server.middlewares.use(async (req, res, next) => {
        if (!req.url?.startsWith("/api/") && req.url !== "/health")
          return next();
        const controller = new AbortController();
        res.on("close", () => controller.abort());
        const hasBody = req.method !== "GET" && req.method !== "HEAD";
        const request = new Request(new URL(req.url, ORIGIN), {
          method: req.method,
          headers: req.headers as Record<string, string>,
          body: hasBody ? (Readable.toWeb(req) as ReadableStream) : undefined,
          signal: controller.signal,
          duplex: "half",
        } as RequestInit);
        const response = await getResponse(handlers, request, {
          baseUrl: ORIGIN,
        });
        if (!response) return next();
        res.writeHead(response.status, Object.fromEntries(response.headers));
        if (!response.body) return res.end();
        Readable.fromWeb(
          response.body as Parameters<typeof Readable.fromWeb>[0],
        )
          .on("error", () => res.end())
          .pipe(res);
      });
    },
  };
}

export default mergeConfig(
  baseConfig,
  defineConfig({ plugins: [mockBackend()] }),
);
