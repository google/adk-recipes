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

// Opt-in: drives the real agent, so it costs inference calls and minutes.
// Enable: SMOKE_LIVE=1 npm run test:e2e -- panel-refresh-live
//
// Reproduces the reported bug: with a workspace file open in the side panel,
// a turn that rewrites that file must refresh the panel without the user
// clicking the file again.
test.describe("side panel refresh after an agent write", () => {
  test.skip(!process.env.SMOKE_LIVE, "set SMOKE_LIVE=1 to enable");
  test.setTimeout(300_000);

  const FILE = "e2e-panel.md";

  test("panel updates when the agent rewrites the open file", async ({
    page,
  }) => {
    // /lha/secrets and /lha/gcp/status 500 unless Secret Manager is enabled
    // on the dev project — unrelated to the panel, so only watch the
    // workspace endpoints this test actually exercises.
    const failures: string[] = [];
    page.on("response", (r) => {
      const u = new URL(r.url()).pathname;
      if (!u.startsWith("/lha/workspace")) return;
      if (r.status() >= 400) failures.push(`${r.status()} ${u}`);
    });

    // Start from a clean slate so a previous run's file can't satisfy the
    // assertions before the agent has written anything.
    await page.request
      .delete(`/lha/workspace/file?path=${encodeURIComponent(FILE)}`)
      .catch(() => {});

    await page.goto("/");
    const composer = page.getByRole("textbox").first();

    // 1. Create the file with a known first heading.
    await composer.fill(
      `Use write_file to create ${FILE} containing exactly this line and nothing else: "# Alpha". Do not save it as an artifact.`,
    );
    await page.keyboard.press("Enter");

    // 2. Open it in the side panel via the sidebar tree. The row's filename
    // button (the sibling anchor shares the title, so constrain to `button`).
    const row = page.locator(`button[title="Open ${FILE}"]`).first();
    await expect(async () => {
      await page.getByRole("button", { name: "Refresh workspace" }).click();
      await expect(row).toBeVisible({ timeout: 2_000 });
    }).toPass({ timeout: 150_000 });
    await row.click();

    const panel = page.getByRole("tab", { name: new RegExp(FILE) });
    await expect(panel).toBeVisible({ timeout: 15_000 });
    await expect(
      page.getByRole("heading", { name: "Alpha" }).last(),
    ).toBeVisible({ timeout: 15_000 });

    // 3. Ask the agent to rewrite it — and do NOT touch the panel after.
    await composer.fill(
      `Now use write_file to replace the entire contents of ${FILE} with exactly: "# Beta". Do not save it as an artifact.`,
    );
    await page.keyboard.press("Enter");

    // 4. The panel must pick it up on its own.
    await expect(
      page.getByRole("heading", { name: "Beta" }).last(),
    ).toBeVisible({ timeout: 180_000 });

    expect(
      failures,
      `workspace request failures: ${failures.join(" | ")}`,
    ).toEqual([]);
  });
});
