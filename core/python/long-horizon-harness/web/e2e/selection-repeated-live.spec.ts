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
// Enable: SMOKE_LIVE=1 npm run test:e2e -- selection-repeated-live
//
// The adversarial case: the highlighted text occurs several times, so no
// content-based anchor can identify which one was meant. Offsets can.
test.describe("selection in a repetitive file", () => {
  test.skip(!process.env.SMOKE_LIVE, "set SMOKE_LIVE=1 to enable");
  test.setTimeout(300_000);

  const FILE = "e2e-repeat.md";
  // "b" appears three times; the middle one is the target.
  const BODY = "a\nb\nc\nb\nd\nb\n";

  test("edits the highlighted occurrence and leaves its twins alone", async ({
    page,
  }) => {
    await page.request
      .delete(`/lha/workspace/file?path=${encodeURIComponent(FILE)}`)
      .catch(() => {});

    await page.goto("/");
    const composer = page.getByRole("textbox").first();
    await composer.fill(
      `Use write_file to create ${FILE} with exactly these lines and nothing else:\n${BODY}Do not save it as an artifact.`,
    );
    await page.keyboard.press("Enter");

    await expect(page.getByRole("tab", { name: new RegExp(FILE) })).toBeVisible(
      {
        timeout: 180_000,
      },
    );
    await page.getByRole("button", { name: /^source$/i }).click();

    // Highlight the SECOND "b" (offset 2), not the first or third.
    const editor = page.locator("textarea").last();
    await editor.evaluate((el: HTMLTextAreaElement) => {
      el.focus();
      const i = el.value.indexOf("b");
      el.setSelectionRange(i, i + 1);
      el.dispatchEvent(new KeyboardEvent("keyup", { bubbles: true }));
    });
    await expect(
      page.getByRole("button", { name: /clear selection/i }),
    ).toBeVisible({ timeout: 10_000 });

    await composer.fill("Replace this with: MIDDLE");
    await page.keyboard.press("Enter");

    await expect(async () => {
      const body = await page.request
        .get(`/lha/workspace/download?path=${encodeURIComponent(FILE)}`)
        .then((r) => r.text());
      // Only the highlighted occurrence changed; the other two survive.
      expect(body.replace(/\s+$/, "")).toBe("a\nMIDDLE\nc\nb\nd\nb");
    }).toPass({ timeout: 180_000 });
  });
});
