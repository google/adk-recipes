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

import { defineConfig, devices } from "@playwright/test";

// The PERF_* env vars override the web URL/command so a perf run can start a
// dev server on a free port pointed at the auth proxy. Defaults are unchanged
// for normal `npm run test:e2e`.
const WEB_URL = process.env.PERF_WEB_URL ?? "http://localhost:3000";

export default defineConfig({
  testDir: "./e2e",
  timeout: 30_000,
  expect: { timeout: 5_000 },
  fullyParallel: false,
  forbidOnly: !!process.env.CI,
  retries: 0,
  reporter: [["list"]],
  use: {
    baseURL: WEB_URL,
    trace: "on-first-retry",
    actionTimeout: 5_000,
  },
  projects: [
    {
      name: "chromium",
      use: { ...devices["Desktop Chrome"] },
    },
  ],
  webServer: {
    command: process.env.PERF_WEB_CMD ?? "npm run dev",
    url: WEB_URL,
    reuseExistingServer: true,
    timeout: 120_000,
  },
});
