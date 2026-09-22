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
// Enable: SMOKE_LIVE=1 npm run test:e2e -- rename-live
test.describe("rename swaps the open tab", () => {
  test.skip(!process.env.SMOKE_LIVE, "set SMOKE_LIVE=1 to enable");
  test.setTimeout(300_000);

  const OLD = "e2e-oldname.md";
  const NEW = "e2e-newname.md";

  test("the old tab closes and the new file takes its place", async ({
    page,
  }) => {
    for (const f of [OLD, NEW]) {
      await page.request
        .delete(`/lha/workspace/file?path=${encodeURIComponent(f)}`)
        .catch(() => {});
    }

    await page.goto("/");
    const composer = page.getByRole("textbox").first();

    // Auto-open puts it in the panel; no clicking required.
    await composer.fill(
      `Use write_file to create ${OLD} with one line about cats. Do not save it as an artifact.`,
    );
    await page.keyboard.press("Enter");
    await expect(page.getByRole("tab", { name: new RegExp(OLD) })).toBeVisible({
      timeout: 180_000,
    });

    await composer.fill(`Rename ${OLD} to ${NEW}.`);
    await page.keyboard.press("Enter");

    await expect(page.getByRole("tab", { name: new RegExp(NEW) })).toBeVisible({
      timeout: 180_000,
    });
    await expect(page.getByRole("tab", { name: new RegExp(OLD) })).toBeHidden();
  });
});
