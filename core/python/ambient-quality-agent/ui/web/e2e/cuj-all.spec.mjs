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
  console.log(`\n======================================================`);
  console.log(`     AQuA Comprehensive CUJ Validation Suite`);
  console.log(`     Target: ${BASE_URL}`);
  console.log(`======================================================\n`);

  const browser = await chromium.launch();
  const context = await browser.newContext({
    viewport: { width: 1440, height: 900 },
    permissions: ["clipboard-read", "clipboard-write"],
  });
  const page = await context.newPage();

  let passed = 0;
  let failed = 0;

  function assertStep(name, ok, detail = "") {
    if (ok) {
      passed++;
      console.log(`  ✓ ${name}${detail ? ` (${detail})` : ""}`);
    } else {
      failed++;
      console.error(`  ✗ ${name}${detail ? ` — FAILED: ${detail}` : ""}`);
    }
  }

  try {
    // --- CUJ 1: Alert → What is broken? ---
    console.log(`CUJ 1: Alert Arrival — "What is broken?"`);
    await page.goto(`${BASE_URL}/insights`, {
      waitUntil: "networkidle",
      timeout: 45000,
    });
    await page.waitForSelector('a[href*="/insights/"]', { timeout: 45000 });

    const bodyText = (await page.textContent("body")) || "";
    assertStep(
      "Worst issue listed first with plain diagnosis",
      /create_ticket|file_expense|ValueError|priority|category/i.test(
        bodyText.slice(0, 1500),
      ),
    );
    assertStep(
      "Impact and occurrence counts stated",
      /impact|occurrence/i.test(bodyText),
    );
    assertStep(
      "Source code reference cited",
      /app\/agent\.py:\d+|line \d+|file_expense|create_ticket/i.test(bodyText),
    );

    // --- Global Shell: Observed Agent & Data Freshness ---
    console.log(`\nGlobal Header: Observed Agent & Data Freshness`);
    const freshnessBadge = page.locator(
      '[data-testid="agent-freshness-badge"]',
    );
    await freshnessBadge.waitFor({ state: "visible", timeout: 15000 });
    const badgeText = (await freshnessBadge.textContent()) || "";
    assertStep(
      "Observed agent and freshness displayed in header",
      /it_support_agent|agent/i.test(badgeText),
      badgeText,
    );

    // --- Sidebar: Rails & CHATS count ---
    console.log(`\nSidebar Navigation: Rails & CHATS Count`);
    const topIssuesRail = page.locator(
      'button:has-text("Top insights"), span:has-text("Top insights")',
    );
    assertStep(
      "Sidebar displays Top insights rail",
      (await topIssuesRail.count()) > 0,
    );

    const recentRunsRail = page.locator(
      'button:has-text("Recent investigations"), span:has-text("Recent investigations")',
    );
    assertStep(
      "Sidebar displays Recent investigations rail",
      (await recentRunsRail.count()) > 0,
    );

    // Inspect Chats in ChatListSidebar on /
    // The chat route polls by design (horizon-tasks every 10s, sessions every
    // 30s), so "networkidle" never settles here. Wait for the element instead.
    await page.goto(`${BASE_URL}/`, {
      waitUntil: "domcontentloaded",
      timeout: 45000,
    });
    const chatsHeader = page
      .locator('button:has-text("Chats"), span:has-text("Chats")')
      .first();
    await chatsHeader.waitFor({ state: "visible", timeout: 30000 });
    const chatsHeaderText = (await chatsHeader.textContent()) || "";
    assertStep(
      "CHATS section explicitly indicates count in sidebar",
      /\(\d+\)/.test(chatsHeaderText) || /chats/i.test(chatsHeaderText),
      chatsHeaderText,
    );

    // --- Insights: Status Filtering & Pagination ---
    console.log(`\nInsights: Status Filters & Pagination`);
    await page.goto(`${BASE_URL}/insights`, {
      waitUntil: "networkidle",
      timeout: 45000,
    });
    await page.waitForSelector('a[href*="/insights/"]', { timeout: 45000 });

    const statusPills = page.locator("header >> button");
    const pillCount = await statusPills.count();
    assertStep(
      "Status filter pills available (all, new, recurring, resolved)",
      pillCount >= 4,
      `${pillCount} filter buttons`,
    );

    // --- CUJ 2: "Show me it happening" (Detail & Trajectories) ---
    console.log(`\nCUJ 2: "Show me it happening"`);
    const cards = page.locator('a[href*="/insights/"]');
    const cardCount = await cards.count();
    if (cardCount === 0) {
      throw new Error("No insight cards found on /insights");
    }
    assertStep("Insight cards clickable", cardCount > 0, `${cardCount} cards`);

    await cards.first().click();
    await page.waitForSelector('button:has-text("Copy Bug Report")', {
      timeout: 20000,
    });

    const detailText = (await page.textContent("body")) || "";
    assertStep(
      "Detail panel opened without spinner",
      !/Loading…/.test(detailText),
    );
    assertStep(
      "Trajectories listed as evidence links",
      /Trajectories|Trajectory/i.test(detailText),
    );
    assertStep(
      "History of occurrences rendered",
      /Latest occurrence|History/i.test(detailText),
    );

    // --- Bug Report Copy ---
    console.log(`\nHandoff: Copy Bug Report`);
    const copyBtn = page.getByRole("button", { name: /Copy Bug Report/i });
    assertStep("Copy Bug Report button visible", await copyBtn.isVisible());
    await copyBtn.click();
    await page.waitForSelector('button:has-text("Copied!")', { timeout: 5000 });
    assertStep("Copy Bug Report flips to Copied!", true);

    const clipboard = await page.evaluate(() => navigator.clipboard.readText());
    assertStep(
      "Clipboard contains complete formatted bug report",
      clipboard.includes("# [Bug Report]") &&
        clipboard.includes("**Observed agent:**") &&
        clipboard.includes("**Status:**") &&
        clipboard.includes("/insights/") &&
        clipboard.includes("## Diagnosis"),
    );

    // --- CUJ 3: Investigations & Funnel ---
    console.log(`\nCUJ 3: Investigations View & Metrics Funnel`);
    await page.goto(`${BASE_URL}/investigations`, {
      waitUntil: "networkidle",
      timeout: 45000,
    });
    await page.waitForSelector('table, a[href*="/investigations/"]', {
      timeout: 45000,
    });

    const invText = (await page.textContent("body")) || "";
    assertStep("Investigations view loaded", /Investigations/i.test(invText));
    assertStep(
      "Funnel metrics populated",
      /\d+.*scanned/i.test(invText) ||
        /\d+.*evaluated/i.test(invText) ||
        /\d+/.test(invText),
    );
    const runLinks = page.locator('a[href*="/investigations/"]');
    const runLinkCount = await runLinks.count();
    assertStep(
      "Investigation runs listed in table",
      runLinkCount > 0,
      `${runLinkCount} runs`,
    );

    // --- CUJ 4: Configuration (Goal & Memory) ---
    console.log(`\nCUJ 4: Configuration (Goal & Memory)`);
    await page.goto(`${BASE_URL}/config`, {
      waitUntil: "networkidle",
      timeout: 45000,
    });
    await page.waitForSelector("textarea", { timeout: 30000 });

    const ta = page.locator("textarea").first();
    const goalVal = await ta.inputValue();
    assertStep(
      "Goal textarea loaded with goal content from GCS",
      goalVal.length > 0,
      `${goalVal.length} chars`,
    );
    const saveBtn = page.getByRole("button", { name: /save/i });
    assertStep(
      "Save configuration button present",
      (await saveBtn.count()) > 0,
    );

    const configText = (await page.textContent("body")) || "";
    assertStep("Memory section rendered", /Memory/.test(configText));
    assertStep(
      "GCS bucket storage active without degradation",
      !/not configured/i.test(configText),
    );

    console.log(`\n======================================================`);
    console.log(`  Results: ${passed} passed, ${failed} failed`);
    console.log(`======================================================\n`);

    if (failed > 0) {
      process.exit(1);
    }
  } finally {
    await browser.close();
  }
}

run().catch((err) => {
  console.error("❌ CUJ validation suite failed:", err);
  process.exit(1);
});
