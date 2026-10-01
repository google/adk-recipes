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

import { expect, test } from "vitest";

// Runnability test: importing the agent module must build the agent graph. It
// never calls the model, so it needs no network access or credentials.
test("rootAgent has the four specialist tools", async () => {
  // Defaults come from .env.example; variables already set take precedence.
  process.loadEnvFile(new URL("../.env.example", import.meta.url));
  const { app, rootAgent } = await import("../app/agent");

  expect(rootAgent.name).toBe("financial_coordinator");
  expect(rootAgent.outputKey).toBe("financial_coordinator_output");
  expect(app.rootAgent).toBe(rootAgent);
  expect(
    rootAgent.tools.map((tool) => (tool as { name: string }).name),
  ).toEqual([
    "data_analyst_agent",
    "trading_analyst_agent",
    "execution_analyst_agent",
    "risk_analyst_agent",
  ]);
});
