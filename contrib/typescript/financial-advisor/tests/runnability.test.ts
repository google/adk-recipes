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
import { AdkApiServer } from "@google/adk-devtools";
import { expect, test } from "vitest";

// Runnability tests, the counterpart of the Python recipe's
// test_runnability.py. Neither calls the model, so they need no network
// access or credentials.

// Defaults come from .env.example; variables already set take precedence.
process.loadEnvFile(new URL("../.env.example", import.meta.url));

test("agent module defines rootAgent and app", async () => {
  const { app, rootAgent } = await import("../app/agent");

  expect(rootAgent).toBeDefined();
  expect(app).toBeDefined();
});

test("ADK server loads the agent and lists it", async () => {
  // The same loader `adk web .` uses: it compiles app/agent.ts from the
  // recipe directory and serves it as "app".
  const server = new AdkApiServer({
    agentsDir: path.resolve(import.meta.dirname, ".."),
    host: "127.0.0.1",
    port: 0,
  });
  await server.start();
  try {
    const response = await fetch(`${server.url}/list-apps`);

    expect(response.status).toBe(200);
    expect(await response.json()).toContain("app");
  } finally {
    await server.stop();
  }
}, 60_000);
