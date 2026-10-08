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

// The rail's resize and collapse need real layout: react-resizable-panels
// asserts on a measured size, so the unit tests cannot drive them.
import { chromium } from "playwright";

const BASE_URL = process.env.AQUA_BASE_URL || "http://localhost:8080";
const out = [];
const check = (name, ok, detail = "") => {
  out.push([name, ok, detail]);
  console.log(
    `${ok ? "PASS" : "FAIL"}  ${name}${detail ? `  (${detail})` : ""}`,
  );
};

const railWidth = (p) =>
  p.evaluate(() => {
    const nav = document.querySelector("nav.sidebar-cq");
    return nav ? Math.round(nav.getBoundingClientRect().width) : 0;
  });

// Headless Chromium hides scrollbars by default; check 9 needs real ones.
const browser = await chromium.launch({
  ignoreDefaultArgs: ["--hide-scrollbars"],
});
const page = await browser.newPage({ viewport: { width: 1400, height: 900 } });
try {
  await page.goto(`${BASE_URL}/insights`, { waitUntil: "domcontentloaded" });
  await page.waitForSelector("nav.sidebar-cq", { timeout: 20000 });

  const initial = await railWidth(page);
  check("1 rail is visible", initial > 100, `${initial}px`);

  // --- drag the handle -----------------------------------------------------
  const handle = page.locator('[role="separator"]').first();
  const box = await handle.boundingBox();
  await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2);
  await page.mouse.down();
  await page.mouse.move(box.x + 160, box.y + box.height / 2, { steps: 12 });
  await page.mouse.up();
  await page.waitForTimeout(400);

  const widened = await railWidth(page);
  check(
    "2 dragging the handle resizes it",
    widened > initial + 60,
    `${initial} -> ${widened}`,
  );

  // --- hide ----------------------------------------------------------------
  await page.getByRole("button", { name: /hide chats/i }).click();
  await page.waitForTimeout(400);
  const hidden = await railWidth(page);
  // A collapsed panel still paints its 1px border; anything under a few px
  // is gone as far as the reader is concerned.
  check("3 hide collapses the rail", hidden < 4, `${hidden}px`);

  // --- where the way back sits ---------------------------------------------
  // It used to float at the page's top-left corner, which put it on top of the
  // wordmark: nothing between it and the viewport is positioned.
  const beside = await page.evaluate(() => {
    const brand = [...document.querySelectorAll("a")].find(
      (a) => a.textContent.trim() === "AQuA",
    );
    const button = document.querySelector('button[aria-label="Show chats"]');
    if (!brand || !button) return null;
    const a = brand.getBoundingClientRect();
    const b = button.getBoundingClientRect();
    return {
      clear: b.left >= a.right || b.right <= a.left,
      rightOf: b.left >= a.right,
    };
  });
  check(
    "4 show sits beside the wordmark, not over it",
    !!beside && beside.clear && beside.rightOf,
    JSON.stringify(beside),
  );

  // --- and back ------------------------------------------------------------
  await page.getByRole("button", { name: /show chats/i }).click();
  await page.waitForTimeout(400);
  const restored = await railWidth(page);
  check("5 show brings it back", restored > 100, `${restored}px`);

  // --- the width survives navigation ---------------------------------------
  await page.goto(`${BASE_URL}/investigations`, {
    waitUntil: "domcontentloaded",
  });
  await page.waitForSelector("nav.sidebar-cq", { timeout: 20000 });
  const afterNav = await railWidth(page);
  check(
    "6 width persists across routes",
    Math.abs(afterNav - restored) < 25,
    `${restored} -> ${afterNav}`,
  );

  // --- a short window scrolls inside the rail, never the page --------------
  // Sections clip their rows; anything that escapes them (an absolutely
  // positioned label with no positioned row around it) stretches the page.
  await page.setViewportSize({ width: 1440, height: 300 });
  await page.goto(`${BASE_URL}/`, { waitUntil: "networkidle" });
  await page.waitForSelector("nav.sidebar-cq", { timeout: 20000 });
  await page.waitForTimeout(800);
  const pageHeight = await page.evaluate(
    () => document.documentElement.scrollHeight,
  );
  check(
    "7 a 300px window does not scroll the page",
    pageHeight <= 300,
    `scrollHeight ${pageHeight}`,
  );

  // Every section open and the rail at its default width, as a first visit
  // sees them: the checks above left it widened and its sections toggled.
  const openAllSections = async (height) => {
    await page.setViewportSize({ width: 1440, height });
    await page.evaluate(() => localStorage.clear());
    await page.goto(`${BASE_URL}/`, { waitUntil: "networkidle" });
    await page.waitForSelector('nav a[href^="/insights/"]', { timeout: 20000 });
    await page.waitForTimeout(800);
  };

  // --- the chevron is part of the header's click target --------------------
  // An open section's chevron is rotated, and a transformed element paints
  // above the toggle's full-header overlay, so it must not take the click.
  await openAllSections(900);
  const issuesToggle = page.getByRole("button", { name: /^Top insights/ });
  const chevron = await issuesToggle.evaluate((b) => {
    const r = b
      .closest("h2")
      .parentElement.querySelector(":scope > svg")
      .getBoundingClientRect();
    return { x: r.x + r.width / 2, y: r.y + r.height / 2 };
  });
  await page.mouse.click(chevron.x, chevron.y);
  await page.waitForTimeout(300);
  check(
    "8 clicking an open section's chevron collapses it",
    (await issuesToggle.getAttribute("aria-expanded")) === "false",
  );

  // --- a list keeps its text width when its scrollbar comes and goes -------
  // Collapsing Recent investigations gives Top insights its room; at some window
  // height that is exactly what makes Top insights stop overflowing.
  const issuesList = () =>
    page.evaluate(() => {
      const ul = document
        .querySelector('nav a[href^="/insights/"]')
        .closest("ul");
      const row = ul.querySelector("a");
      return {
        overflow: ul.scrollHeight > ul.clientHeight,
        width: row.clientWidth,
      };
    });
  let flip = null;
  for (let height = 700; height <= 1600 && !flip; height += 50) {
    await openAllSections(height);
    const withRuns = await issuesList();
    if (!withRuns.overflow) continue;
    await page.getByRole("button", { name: /^Recent investigations/ }).click();
    await page.waitForTimeout(400);
    const withoutRuns = await issuesList();
    if (!withoutRuns.overflow) flip = { height, withRuns, withoutRuns };
  }
  check(
    "9 a list that stops overflowing keeps its row width",
    flip !== null && flip.withRuns.width === flip.withoutRuns.width,
    flip
      ? `at ${flip.height}px: width ${flip.withRuns.width} -> ${flip.withoutRuns.width}`
      : "no window height made the list stop overflowing",
  );
} finally {
  await browser.close();
}

const failed = out.filter(([, ok]) => !ok);
console.log(`\n${out.length - failed.length}/${out.length} passed`);
process.exit(failed.length ? 1 : 0);
