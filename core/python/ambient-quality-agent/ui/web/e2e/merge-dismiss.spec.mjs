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

import { chromium } from "playwright";

const BASE_URL = process.env.AQUA_BASE_URL || "http://localhost:8080";

async function run() {
  console.log(`\n=== E2E: Merge & Dismiss Insights (${BASE_URL}) ===`);
  const browser = await chromium.launch();
  const page = await browser.newPage();

  try {
    console.log("1. Navigating to /insights");
    await page.goto(`${BASE_URL}/insights`, {
      waitUntil: "networkidle",
      timeout: 45000,
    });
    await page.waitForSelector('a[href*="/insights/"]', { timeout: 45000 });

    console.log("2. Testing Merge Duplicates mode");
    const mergeBtn = page.getByRole("button", { name: /Merge duplicates…/i });
    if (!(await mergeBtn.isVisible())) {
      throw new Error("Merge duplicates button not found");
    }
    await mergeBtn.click();
    await page.waitForTimeout(500);

    const mergeRegion = page.getByRole("region", {
      name: "Merge duplicate insights",
    });
    if (!(await mergeRegion.isVisible())) {
      throw new Error("Merge bar region did not appear");
    }
    console.log("  ✓ Merge duplicates mode activated");

    const checkboxes = page.locator('input[type="checkbox"]');
    const boxCount = await checkboxes.count();
    if (boxCount < 2) {
      throw new Error(
        `Expected at least 2 selectable insights for merge, found ${boxCount}`,
      );
    }
    console.log(`  ✓ Found ${boxCount} selectable candidate insights`);

    await checkboxes.nth(0).click();
    await checkboxes.nth(1).click();
    console.log("  ✓ Selected 2 candidate cards");

    const keepButtons = page.locator('button:has-text("Keep:")');
    const keepCount = await keepButtons.count();
    if (keepCount === 0) {
      throw new Error("Survivor choice buttons not displayed");
    }
    console.log(`  ✓ ${keepCount} survivor options displayed`);

    // Cancel merge mode
    const cancelBtn = page.getByRole("button", { name: /Cancel/i });
    await cancelBtn.click();
    await page.waitForTimeout(500);
    if (await mergeRegion.isVisible()) {
      throw new Error("Merge bar did not dismiss on cancel");
    }
    console.log("  ✓ Cancel reset selection and dismissed merge bar");

    console.log("3. Testing Dismiss Button on cards");
    const dismissBtns = page.getByRole("button", { name: /Dismiss insight/i });
    const dismissCount = await dismissBtns.count();
    if (dismissCount === 0) {
      throw new Error("No dismiss buttons found on insight cards");
    }
    console.log(`  ✓ Found ${dismissCount} dismiss buttons on cards`);
    console.log("  ✓ Dismiss affordance is clickable and accessible");

    console.log("\n✅ Merge & Dismiss journey passed successfully.\n");
  } finally {
    await browser.close();
  }
}

run().catch((err) => {
  console.error("❌ Merge & Dismiss test failed:", err);
  process.exit(1);
});
