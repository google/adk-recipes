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
  console.log(`\n=== E2E: Bug Report Export (${BASE_URL}) ===`);
  const browser = await chromium.launch();
  const context = await browser.newContext({
    permissions: ["clipboard-read", "clipboard-write"],
  });
  const page = await context.newPage();

  try {
    console.log("1. Navigating to /insights");
    await page.goto(`${BASE_URL}/insights`, {
      waitUntil: "networkidle",
      timeout: 45000,
    });
    await page.waitForSelector('a[href*="/insights/"]', { timeout: 45000 });

    const cards = page.locator('a[href*="/insights/"]');
    const count = await cards.count();
    if (count === 0) {
      throw new Error("No insight cards found on /insights");
    }
    console.log(`  ✓ Found ${count} insight cards`);

    console.log("2. Opening the top insight detail");
    await cards.first().click();
    await page.waitForSelector('button:has-text("Copy Bug Report")', {
      timeout: 15000,
    });
    console.log("  ✓ Insight detail loaded with Copy Bug Report button");

    console.log("3. Clicking 'Copy Bug Report'");
    const copyBtn = page.getByRole("button", { name: /Copy Bug Report/i });
    await copyBtn.click();
    await page.waitForSelector('button:has-text("Copied!")', { timeout: 5000 });
    console.log("  ✓ Button changed state to 'Copied!'");

    // Verify clipboard content
    const clipboardContent = await page.evaluate(() =>
      navigator.clipboard.readText(),
    );
    console.log("4. Verifying formatted markdown report in clipboard:");
    console.log(clipboardContent.slice(0, 300) + "...\n");

    if (!clipboardContent || clipboardContent.length < 50) {
      throw new Error("Clipboard content was empty or unexpectedly short");
    }
    if (!clipboardContent.includes("# [Bug Report]")) {
      throw new Error("Missing '# [Bug Report]' header in clipboard content");
    }
    if (!clipboardContent.includes("**Observed agent:**")) {
      throw new Error("Missing '**Observed agent:**' field");
    }
    if (!clipboardContent.includes("**Status:**")) {
      throw new Error("Missing '**Status:**' field");
    }
    if (
      !clipboardContent.includes("**Permalink:**") ||
      !clipboardContent.includes("/insights/")
    ) {
      throw new Error("Missing '**Permalink:**' field with /insights/ route");
    }
    if (!clipboardContent.includes("## Diagnosis")) {
      throw new Error("Missing '## Diagnosis' section");
    }
    console.log("  ✓ All bug report markdown fields present and valid");

    console.log("\n✅ Bug report journey passed successfully.\n");
  } finally {
    await browser.close();
  }
}

run().catch((err) => {
  console.error("❌ Bug report test failed:", err);
  process.exit(1);
});
