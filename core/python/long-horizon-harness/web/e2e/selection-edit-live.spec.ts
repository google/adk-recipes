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
// Enable: SMOKE_LIVE=1 npm run test:e2e -- selection-edit-live
test.describe("selection-to-agent edit", () => {
  test.skip(!process.env.SMOKE_LIVE, "set SMOKE_LIVE=1 to enable");
  test.setTimeout(300_000);

  const FILE = "e2e-selection.md";

  test("agent rewrites only the highlighted span", async ({ page }) => {
    await page.request
      .delete(`/lha/workspace/file?path=${encodeURIComponent(FILE)}`)
      .catch(() => {});

    await page.goto("/");
    const composer = page.getByRole("textbox").first();

    await composer.fill(
      `Use write_file to create ${FILE} with exactly these three lines and nothing else:\nKEEP ONE\nTARGET LINE\nKEEP TWO\nDo not save it as an artifact.`,
    );
    await page.keyboard.press("Enter");

    const row = page.locator(`button[title="Open ${FILE}"]`).first();
    await expect(async () => {
      await page.getByRole("button", { name: "Refresh workspace" }).click();
      await expect(row).toBeVisible({ timeout: 2_000 });
    }).toPass({ timeout: 150_000 });
    await row.click();

    await page.getByRole("button", { name: /^source$/i }).click();

    // Highlight the middle line. React implements onSelect through its
    // SelectEventPlugin, which watches focus/keyup/mouseup and diffs the
    // selection — a raw `select` event does not reach it. Focus, set the
    // range, then emit keyup, which is what a keyboard selection produces.
    const editor = page.locator("textarea").last();
    await editor.evaluate((el: HTMLTextAreaElement) => {
      el.focus();
      const i = el.value.indexOf("TARGET LINE");
      el.setSelectionRange(i, i + "TARGET LINE".length);
      el.dispatchEvent(new KeyboardEvent("keyup", { bubbles: true }));
    });

    // The chip proves the anchor was captured before we type the instruction.
    await expect(
      page.getByRole("button", { name: /clear selection/i }),
    ).toBeVisible({ timeout: 10_000 });

    await composer.fill("Replace this with: REWRITTEN");
    await page.keyboard.press("Enter");

    await expect(async () => {
      const body = await page.request
        .get(`/lha/workspace/download?path=${encodeURIComponent(FILE)}`)
        .then((r) => r.text());
      expect(body).toContain("REWRITTEN");
      expect(body).toContain("KEEP ONE");
      expect(body).toContain("KEEP TWO");
      expect(body).not.toContain("TARGET LINE");
    }).toPass({ timeout: 180_000 });
  });
});
