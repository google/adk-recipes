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

// The dashboard homepage, against whatever the deployment really holds.
import { chromium } from "playwright";

const BASE_URL = process.env.AQUA_BASE_URL || "http://localhost:8080";
const out = [];
const check = (name, ok, detail = "") => {
  out.push(ok);
  console.log(
    `${ok ? "PASS" : "FAIL"}  ${name}${detail ? `  (${detail})` : ""}`,
  );
};

const browser = await chromium.launch();
const page = await browser.newPage({ viewport: { width: 1500, height: 1100 } });
try {
  const api = await (await page.request.get(`${BASE_URL}/api/health`)).json();
  const answered = !api.error && Boolean(api.verdict);
  check(
    "1 /api/health answers",
    answered,
    api.error || api.verdict || "no verdict",
  );
  if (!answered) {
    console.log("\nhealth payload unusable; skipping the checks that read it");
    await browser.close();
    process.exit(1);
  }

  await page.goto(BASE_URL, { waitUntil: "domcontentloaded" });
  const bar = page.getByRole("region", { name: /ambient health/i });
  await bar.waitFor({ timeout: 25000 });
  check("2 the stat bar is the first thing on the homepage", true);

  const barText = (await bar.innerText()).replace(/\n+/g, " · ");
  const shown = {
    watching: /Watching/i,
    overdue: /Overdue/i,
    never: /Never run/i,
    failing: /Failing/i,
    disabled: /Disabled/i,
  }[api.verdict];
  check(
    "3 it shows the real verdict",
    Boolean(shown) && shown.test(barText),
    `${api.verdict}: ${barText}`,
  );

  // Every section resolves to data or to a stated empty, never to a stale
  // "nothing yet" that is really "still loading".
  await page.waitForTimeout(9000);
  for (const [name, heading] of [
    ["4 pipeline", "Pipeline"],
    ["5 activity", "Activity"],
    ["6 insights", "Insights"],
    ["7 recent investigations", "Recent investigations"],
  ]) {
    const section = page
      .locator("section")
      .filter({ hasText: heading })
      .first();
    const text = await section.innerText();
    const stillLoading = await section.locator(".animate-pulse").count();
    check(
      name,
      stillLoading === 0,
      stillLoading ? "still skeleton after 9s" : text.split("\n")[2] || "",
    );
  }

  // The ask bar has to reach the chat and actually send, not just navigate.
  const question = "How many open findings are there?";
  await page.getByLabel("Ask AQuA").fill(question);
  await page.getByRole("button", { name: "Send", exact: true }).click();
  await page.waitForURL(/\/c\?/, { timeout: 15000 });
  check(
    "8 the ask bar hands the question to the chat",
    true,
    new URL(page.url()).search,
  );

  await page.waitForSelector(`text=${question}`, { timeout: 40000 });
  // Count bubbles only: the sidebar entry and the chat title legitimately
  // repeat the text, so a bare page-wide count reads 3 for one message.
  const bubbles = await page
    .locator(".msg-prose", { hasText: question })
    .count();
  check("9 sent exactly once", bubbles === 1, `${bubbles} bubble(s)`);

  const left = await page
    .getByPlaceholder(/ask anything|connecting/i)
    .inputValue();
  check(
    "10 the composer is not left holding it",
    left === "",
    JSON.stringify(left),
  );
} finally {
  await browser.close();
}

const failed = out.filter((ok) => !ok).length;
console.log(`\n${out.length - failed}/${out.length} passed`);
process.exit(failed ? 1 : 0);
