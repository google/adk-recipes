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

// The homepage's health strip, against whatever the deployment really is.
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
const page = await browser.newPage({ viewport: { width: 1500, height: 1000 } });
try {
  const api = await (await page.request.get(`${BASE_URL}/api/health`)).json();
  const answered = !api.error && Boolean(api.verdict);
  check(
    "1 /api/health answers",
    answered,
    api.error || api.verdict || "no verdict",
  );
  // Everything below reads the payload. Without this the checks for a broken
  // deployment throw instead of failing, which is the one case they are for.
  if (!answered) {
    console.log("\nhealth payload unusable; skipping the checks that read it");
    await browser.close();
    process.exit(1);
  }

  await page.goto(BASE_URL, { waitUntil: "domcontentloaded" });
  const panel = page.getByRole("region", { name: /ambient health/i });
  await panel.waitFor({ timeout: 25000 });
  check("2 the panel is on the homepage", true);

  const text = (await panel.innerText()).replace(/\n+/g, " · ");
  // The verdict on screen must be the verdict the API gave, not a default.
  const shown = {
    watching: /Watching/i,
    overdue: /Overdue/i,
    never: /Never run/i,
    failing: /Failing/i,
    disabled: /Disabled/i,
  }[api.verdict];
  check(
    "3 it shows the real verdict",
    Boolean(shown) && shown.test(text),
    `${api.verdict}: ${text}`,
  );

  // Absence must not render as a number.
  if (api.last_run?.pass_rate == null) {
    check("4 no pass rate is invented", !/% passed/.test(text));
  } else {
    const pct = Math.round(api.last_run.pass_rate * 100);
    check(
      "4 the pass rate matches the API",
      text.includes(`${pct}%`),
      `${pct}%`,
    );
  }

  // The homepage ask bar or the /c composer survives alongside the health strip.
  const askOrComposer =
    (await page.getByLabel("Ask AQuA").count()) +
    (await page.getByPlaceholder(/ask anything|connecting/i).count());
  check("5 the chat entry is still there", askOrComposer > 0);

  await page.goto(`${BASE_URL}/insights`, { waitUntil: "domcontentloaded" });
  await page.waitForTimeout(1500);
  const elsewhere = await page
    .getByRole("region", { name: /ambient health/i })
    .count();
  check("6 it stays off other routes", elsewhere === 0);
} finally {
  await browser.close();
}

const failed = out.filter((ok) => !ok).length;
console.log(`\n${out.length - failed}/${out.length} passed`);
process.exit(failed ? 1 : 0);
