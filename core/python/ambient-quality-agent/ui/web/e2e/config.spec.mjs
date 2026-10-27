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
  console.log(`\n=== E2E: Config (Goal & Memory) (${BASE_URL}) ===`);
  const browser = await chromium.launch();
  const page = await browser.newPage();

  try {
    console.log("1. Navigating to /config");
    await page.goto(`${BASE_URL}/config`, {
      waitUntil: "networkidle",
      timeout: 45000,
    });
    await page.waitForSelector("textarea", { timeout: 30000 });

    const ta = page.locator("textarea").first();
    const initialGoal = await ta.inputValue();
    if (initialGoal.length === 0) {
      throw new Error("Goal textarea was empty on /config");
    }
    console.log(
      `  ✓ Goal loaded from GCS (length: ${initialGoal.length} chars)`,
    );

    const bodyText = (await page.textContent("body")) || "";
    if (/not configured/i.test(bodyText)) {
      throw new Error("GCS storage reported not configured on /config");
    }
    console.log("  ✓ GCS storage configured and active");

    console.log("2. Testing save roundtrip through the UI");
    const testStamp = `E2E automated test update at ${Date.now()}`;
    const newText = initialGoal + `\n\n${testStamp}`;
    await ta.fill(newText);

    const saveBtn = page.getByRole("button", { name: /save/i }).first();
    await saveBtn.click();
    await page.waitForTimeout(3000);

    console.log("3. Reloading page to verify persistence");
    await page.reload({ waitUntil: "networkidle" });
    await page.waitForSelector("textarea", { timeout: 30000 });

    const reloadedText = await page.locator("textarea").first().inputValue();
    if (!reloadedText.includes(testStamp)) {
      throw new Error("Saved goal text did not survive page reload");
    }
    console.log("  ✓ Goal edit persisted to GCS and reloaded cleanly");

    // Clean up edit: restore initial goal or safe default if initial was empty
    const cleanupGoal =
      initialGoal && initialGoal.trim().length > 0
        ? initialGoal
        : "# IT Support Agent Goal\n\nHandle incoming support tickets with priority High, Medium, or Low.";
    await ta.fill(cleanupGoal);
    await saveBtn.click();
    await page.waitForTimeout(3000);
    console.log("  ✓ Cleaned up test update");

    console.log("\n✅ Config journey passed successfully.\n");
  } finally {
    await browser.close();
  }
}

run().catch((err) => {
  console.error("❌ Config test failed:", err);
  process.exit(1);
});
