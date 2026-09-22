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

import { expect, test } from "@playwright/test";

// Opt-in: drives the real agent.
// Enable: SMOKE_LIVE=1 npm run test:e2e -- auto-open-live
test.describe("auto-open a written file", () => {
  test.skip(!process.env.SMOKE_LIVE, "set SMOKE_LIVE=1 to enable");
  test.setTimeout(300_000);

  const FILE = "e2e-autoopen.md";

  test("the panel opens the file the turn wrote, with no click", async ({
    page,
  }) => {
    await page.request
      .delete(`/lha/workspace/file?path=${encodeURIComponent(FILE)}`)
      .catch(() => {});

    await page.goto("/");
    await page
      .getByRole("textbox")
      .first()
      .fill(
        `Use write_file to create ${FILE} with a one-line report about animals. Do not save it as an artifact.`,
      );
    await page.keyboard.press("Enter");

    // No interaction with the panel at all — it must open itself.
    await expect(page.getByRole("tab", { name: new RegExp(FILE) })).toBeVisible(
      { timeout: 180_000 },
    );
  });
});
