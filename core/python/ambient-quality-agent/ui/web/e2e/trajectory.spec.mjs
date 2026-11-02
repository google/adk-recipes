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
  console.log(`\n=== E2E: Trajectory & Step Ledger (${BASE_URL}) ===`);
  const browser = await chromium.launch();
  const page = await browser.newPage();

  try {
    console.log("1. Navigating to /investigations");
    await page.goto(`${BASE_URL}/investigations`, {
      waitUntil: "networkidle",
      timeout: 45000,
    });
    await page.waitForSelector('table, a[href*="/investigations/"]', {
      timeout: 45000,
    });

    const runLinks = page.locator('a[href*="/investigations/"]');
    const runCount = await runLinks.count();
    if (runCount === 0) {
      throw new Error("No investigation run links found on /investigations");
    }
    console.log(`  ✓ Found ${runCount} investigation run links`);

    // Most recent runs might be empty sweeps that produced no ledger.
    // Walk the runs until we find one that actually has ledger data.
    console.log("2. Opening an investigation run that has ledger data");
    let opened = false;
    let ledgerCount = 0;
    for (let i = 0; i < Math.min(runCount, 12); i++) {
      const link = runLinks.nth(i);
      const row = (await link.textContent()) || "";
      if (/no data/i.test(row)) continue;
      await link.click();
      await page.waitForSelector("h1, h2", { timeout: 30000 });
      await page.waitForTimeout(2000);

      const rows = page.locator('[data-testid="ledger-row"]');
      const count = await rows.count();
      if (count > 0) {
        opened = true;
        ledgerCount = count;
        break;
      }
      await page.goBack({ waitUntil: "networkidle" });
      await page.waitForSelector('a[href*="/investigations/"]', {
        timeout: 30000,
      });
    }

    if (!opened || ledgerCount === 0) {
      throw new Error(
        "Could not find any investigation run with non-empty ledger rows",
      );
    }
    console.log(
      `  ✓ Investigation run detail page loaded with ${ledgerCount} step ledger rows`,
    );

    console.log("3. Inspecting a trajectory event row");
    const rows = page.locator('[data-testid="ledger-row"]');
    await rows.first().click();
    await page.waitForSelector('[role="tablist"], button[role="tab"]', {
      timeout: 10000,
    });
    const tabList = page.locator('[role="tablist"], button[role="tab"]');
    const tabCount = await tabList.count();
    if (tabCount === 0) {
      throw new Error("Trajectory detail drawer did not open tab list");
    }
    console.log(`  ✓ Trajectory detail drawer opened with ${tabCount} tabs`);

    console.log("4. Checking trajectory link from an insight");
    await page.goto(`${BASE_URL}/insights`, {
      waitUntil: "networkidle",
      timeout: 45000,
    });
    await page.waitForSelector('a[href*="/insights/"]', { timeout: 45000 });

    const insightLinks = page.locator('a[href*="/insights/"]');
    const insightCount = await insightLinks.count();
    if (insightCount === 0) {
      throw new Error("No insight links found on /insights");
    }

    let foundCaseLinks = false;
    for (let i = 0; i < Math.min(insightCount, 5); i++) {
      await insightLinks.nth(i).click();
      await page.waitForSelector("h1", { timeout: 20000 });
      await page.waitForTimeout(1500);

      const caseLinks = page.locator('a[href*="/cases/"]');
      const caseCount = await caseLinks.count();
      if (caseCount > 0) {
        console.log(
          `  ✓ Found ${caseCount} failing conversation evidence links on insight #${i + 1}`,
        );
        console.log("5. Navigating to trajectory view");
        const targetHref = await caseLinks.first().getAttribute("href");
        console.log(`  ✓ Clicking trajectory link (href: ${targetHref})`);
        await caseLinks.first().click();
        await page.waitForURL((url) => url.pathname.includes("/cases/"), {
          timeout: 30000,
        });
        await page.waitForSelector("text=Trajectory", {
          timeout: 30000,
        });
        await page.waitForTimeout(1000);

        const currentUrl = page.url();
        if (!currentUrl.includes("/cases/")) {
          throw new Error(
            `Expected URL to include '/cases/', but got '${currentUrl}'`,
          );
        }
        console.log(`  ✓ URL navigated to trajectory view: ${currentUrl}`);

        const caseText = (await page.textContent("body")) || "";
        if (!caseText.includes("Trajectory")) {
          throw new Error("Missing 'Trajectory' header on case page");
        }
        // "Timeline Replay" only renders once the case conversation resolves
        // (the section is gated on `data?.case`). The endpoint is fine -- a
        // real case returns 200 with a `case` body -- but a stale or evidence-
        // less case id yields an empty page, so wait for it rather than
        // sampling the DOM the instant the URL changes.
        await page
          .waitForSelector("text=Timeline Replay", { timeout: 30000 })
          .catch(() => {});
        const settled = (await page.textContent("body")) || "";
        if (!settled.includes("Timeline Replay")) {
          throw new Error(
            "Missing 'Timeline Replay' on the case page. The route renders it " +
              "only when the case conversation loads, so check " +
              "/api/investigations/<run>/cases/<case> for this case id.",
          );
        }
        if (!settled.includes("Step Ledger")) {
          throw new Error("Missing 'Step Ledger' section on case page");
        }
        console.log(
          "  ✓ Case conversation trajectory & step ledger rendered successfully",
        );
        foundCaseLinks = true;
        break;
      }
      await page.goBack({ waitUntil: "networkidle" });
      await page.waitForSelector('a[href*="/insights/"]', { timeout: 30000 });
    }

    if (!foundCaseLinks) {
      throw new Error("No trajectory links found on any inspected insight");
    }

    console.log("\n✅ Trajectory journey passed successfully.\n");
  } finally {
    await browser.close();
  }
}

run().catch((err) => {
  console.error("❌ Trajectory test failed:", err);
  process.exit(1);
});
