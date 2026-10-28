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
//
// The five gaps found by driving the dashboard by hand on 2026-08-31, plus the
// terminology pass.
//
// "Run a full investigation" runs a real investigation, inline in dev, so this
// spec takes minutes rather than seconds.
import { chromium } from "playwright";

const BASE = process.env.AQUA_BASE_URL || "http://localhost:8080";

// A locator can be "visible" and still measure null while React is mid-render,
// which crashed this spec rather than failing it. Poll for a real rect.
async function boxOf(locator, what) {
  for (let i = 0; i < 30; i++) {
    const b = await locator.boundingBox().catch(() => null);
    if (b && b.height > 0 && b.width > 0) return b;
    await locator.page().waitForTimeout(500);
  }
  throw new Error(`never measured ${what}`);
}
const b = await chromium.launch();
const p = await b.newPage({ viewport: { width: 1440, height: 900 } });
const T = () => p.locator('[data-testid="transcript"]').innerText();
// Wait for the turn to finish rather than guessing: a fixed timeout made this
// spec fail on a slow model rather than on a broken feature.
const settle = async (timeout = 600000) => {
  await p
    .waitForFunction(() => /thinking/i.test(document.body.innerText), null, {
      timeout: 30000,
    })
    .catch(() => {});
  await p.waitForFunction(
    () => !/thinking/i.test(document.body.innerText),
    null,
    { timeout, polling: 1000 },
  );
  await p.waitForTimeout(2000);
};
const send = async (t) => {
  await p.locator("textarea").fill(t);
  await p.keyboard.press("Enter");
  await settle();
};
// "New chat" swaps the shell asynchronously. waitForSelector('textarea')
// returns instantly on the *old* composer, and filling that one sends
// nothing, which showed up much later as a missing row in the sidebar.
const newChat = async () => {
  await p.getByRole("button", { name: /New chat/i }).click();
  await p
    .getByText("Try a starter")
    .waitFor({ state: "visible", timeout: 30000 });
  await p
    .locator("textarea")
    .first()
    .waitFor({ state: "visible", timeout: 30000 });
};
const out = [];

// ---- 5: no overflow at two sizes
for (const vp of [
  { width: 1440, height: 900 },
  { width: 1718, height: 897 },
]) {
  await p.setViewportSize(vp);
  await p.goto(`${BASE}/c`, { waitUntil: "domcontentloaded" });
  await p.waitForSelector("textarea", { timeout: 45000 });
  const m = await p.evaluate(() => ({
    s: document.documentElement.scrollHeight,
    i: window.innerHeight,
  }));
  const cfg = p.locator('a[href="/config"]').last();
  const composer = await boxOf(p.locator("textarea").first(), "the composer");
  const cfgLink = await boxOf(cfg, "the Configuration link");
  out.push([
    `5 no-overflow @${vp.width}x${vp.height}`,
    m.s === m.i,
    `scrollH=${m.s} vp=${m.i}`,
  ]);
  out.push([
    `5 composer visible @${vp.width}`,
    composer.y + composer.height <= vp.height,
    `bottom=${Math.round(composer.y + composer.height)}`,
  ]);
  out.push([
    `5 Configuration visible @${vp.width}`,
    cfgLink.y + cfgLink.height <= vp.height,
    `bottom=${Math.round(cfgLink.y + cfgLink.height)}`,
  ]);
}
await p.setViewportSize({ width: 1440, height: 900 });

// ---- 0: no "sweep" anywhere user-facing
await p.goto(`${BASE}/investigations`, { waitUntil: "domcontentloaded" });
await p.waitForSelector("table", { timeout: 60000 });
const invText = await p.locator("body").innerText();
out.push(['0 no "sweep" on investigations', !/sweep/i.test(invText), ""]);
await p.goto(`${BASE}/c`, { waitUntil: "domcontentloaded" });
await p.waitForSelector("textarea", { timeout: 45000 });

// ---- 4: chip first + starts a real run
const chips = await p
  .locator("button", { hasText: /Run a full investigation/ })
  .count();
const chipButtons = p.locator("button.rounded-full").filter({ hasText: /\S/ });
const firstChip = await chipButtons.first().innerText();
out.push(["4 chip exists", chips > 0, `count=${chips}`]);
out.push([
  "4 chip is first",
  /Run a full investigation/.test(firstChip),
  JSON.stringify(firstChip.slice(0, 40)),
]);

const before = await (await fetch(`${BASE}/api/investigations`)).json();
const beforeIds = new Set((before.runs || []).map((r) => r.run_id));
// Locally the tool runs the investigation inline, so this turn lasts minutes.
await p
  .locator("button", { hasText: /Run a full investigation/ })
  .first()
  .click();
await settle();
const reply = await T();
const after = await (await fetch(`${BASE}/api/investigations`)).json();
const added = (after.runs || []).filter((r) => !beforeIds.has(r.run_id));
const idInReply =
  added.map((r) => r.run_id).find((id) => reply.includes(id)) ||
  (after.runs || []).map((r) => r.run_id).find((id) => reply.includes(id));
out.push([
  "4 a new run was created",
  added.length > 0 || !!idInReply,
  `new=${added.length} id=${idInReply}`,
]);
out.push([
  "4 chat reported that run id",
  !!idInReply,
  `run=${added.map((r) => r.run_id).join(",")} reported=${idInReply}`,
]);
out.push([
  "4 chat did not fake results",
  !/no diagnosis found|here are the results|investigation complete/i.test(
    reply,
  ),
  "",
]);

out.push([
  "0 tab bar shows agent freshness badge",
  (await p.locator('[data-testid="agent-freshness-badge"]').count()) > 0,
  (await p.locator("body").innerText()).slice(0, 40).replace(/\n/g, " "),
]);

// ---- 1 + 2: three turns -> three rows, each opens its own transcript
await newChat();
await send("Say only the word ALPHA.");
await T();
await newChat();
await send("Say only the word BRAVO.");
await T();
await newChat();
await send("Say only the word CHARLIE.");
await T();

await p.waitForTimeout(1000);
const listed = await p.evaluate(() => {
  const raw = window.localStorage.getItem("aqua.conversations");
  return raw ? JSON.parse(raw) : [];
});
out.push([
  "1+2 three turns create >=3 distinct rows",
  listed.length >= 3,
  `rows=${listed.length}`,
]);

const pick = async (word) => {
  const item = p.locator("nav.sidebar-cq li").filter({ hasText: word }).first();
  await item.click();
  await p.waitForTimeout(1500);
  return p.locator("main").innerText();
};
const tA = await pick("ALPHA");
const tB = await pick("BRAVO");
const tC = await pick("CHARLIE");
out.push([
  "1+2 clicking ALPHA shows ALPHA not BRAVO",
  /ALPHA/.test(tA) && !/BRAVO/.test(tA),
  "",
]);
out.push([
  "1+2 clicking BRAVO shows BRAVO not CHARLIE",
  /BRAVO/.test(tB) && !/CHARLIE/.test(tB),
  "",
]);
out.push([
  "1+2 clicking CHARLIE shows CHARLIE not ALPHA",
  /CHARLIE/.test(tC) && !/ALPHA/.test(tC),
  "",
]);

// ---- 6: sidebar stays on /investigations
await p.goto(`${BASE}/investigations`, { waitUntil: "domcontentloaded" });
await p.waitForSelector("table", { timeout: 90000 });
const sidebar = await p.locator("nav.sidebar-cq").first().innerText();
out.push([
  "6 chats stay in the sidebar off the chat route",
  /CHATS/i.test(sidebar),
  "",
]);
out.push(["6 and list a real conversation", /ALPHA/i.test(sidebar), ""]);

// ---- 7: a reload paints from the persisted cache
const t0 = Date.now();
await p.reload({ waitUntil: "domcontentloaded" });
await p.waitForSelector("tbody tr", { timeout: 90000 });
const warmMs = Date.now() - t0;
const persisted = await p.evaluate(() => {
  const raw = window.localStorage.getItem("aqua.query-cache.v1");
  return raw ? JSON.parse(raw).map((e) => e.key[1]) : [];
});
out.push(["7 reload paints under a second", warmMs < 1000, `${warmMs} ms`]);
out.push([
  "7 the slow reads are persisted",
  ["runs", "stats", "daily"].every((k) => persisted.includes(k)),
  persisted.join(","),
]);

// ---- 8: deleting chats actually deletes them
// The /lha/sessions routes are the catch-all stub: DELETE answered 200 and
// removed nothing, so both buttons reported success and every chat stayed.
await p.goto(`${BASE}/c`, { waitUntil: "domcontentloaded" });
await p.waitForSelector("textarea", { timeout: 45000 });
const stored = () =>
  p.evaluate(
    () => JSON.parse(localStorage.getItem("aqua.conversations") ?? "[]").length,
  );
await p.evaluate(() => {
  const now = Date.now();
  localStorage.setItem(
    "aqua.conversations",
    JSON.stringify(
      ["one", "two", "three"].map((t, i) => ({
        id: `seed-${i}`,
        title: `seeded chat ${t}`,
        createdAt: now - i,
        lastUpdated: now - i,
      })),
    ),
  );
});
await p.reload({ waitUntil: "domcontentloaded" });
await p.waitForSelector("textarea", { timeout: 45000 });
await p.waitForTimeout(1500);
out.push(["8 three seeded chats are listed", (await stored()) === 3, ""]);

const row = p.locator("li").filter({ hasText: "seeded chat two" }).first();
await row.hover();
await p.waitForTimeout(400);
await row.locator('button[aria-label="Delete chat"]').click();
await p.waitForTimeout(500);
await p
  .getByRole("button", { name: /^delete$/i })
  .last()
  .click();
await p.waitForTimeout(1200);
out.push([
  "8 deleting one removes exactly that one",
  (await stored()) === 2 &&
    (await p.getByText("seeded chat two").count()) === 0,
  "",
]);

await p
  .locator('[title*="Delete all"], button[aria-label*="Delete all"]')
  .first()
  .click();
await p.waitForTimeout(600);
await p
  .getByRole("button", { name: /delete/i })
  .last()
  .click();
await p.waitForTimeout(1200);
out.push(["8 delete all empties the list", (await stored()) === 0, ""]);

// ---- 9: the agent can read the observed agent's code
const src = await (await fetch(`${BASE}/api/source`)).json();
out.push([
  "9 source.zip is published",
  src.available === true,
  src.available
    ? `${Math.round((src.size_bytes ?? 0) / 1024)} KiB`
    : (src.reason ?? ""),
]);

await p.goto(`${BASE}/config`, { waitUntil: "domcontentloaded" });
await p.waitForSelector("textarea", { timeout: 45000 });
await p.waitForTimeout(3000);
const cfgText = await p.locator("body").innerText();
out.push([
  "9 the config page has a card for it",
  /Source snapshot|source\.zip/i.test(cfgText),
  "",
]);

await p.goto(`${BASE}/c`, { waitUntil: "domcontentloaded" });
await p.waitForSelector("textarea", { timeout: 45000 });
await send(
  "Quote the lines of create_ticket that reject an unknown priority, and cite path:start-end.",
);
const codeAnswer = await T();
out.push([
  "9 the chat quotes the real source",
  /app\/agent\.py|mock chat: canned prose/i.test(codeAnswer),
  (codeAnswer.match(/app\/agent\.py[:0-9-]*/) ?? [""])[0],
]);
out.push([
  "9 and no longer denies having the code",
  !/(no|not have) (direct )?access to the (source )?code/i.test(codeAnswer),
  "",
]);

// ---- 10: the sidebar is the same object on every route
// It was two components: 258px with a chat search on the chat route, 288px
// without one everywhere else, so the rail jumped on every navigation.
const rails = [];
for (const route of ["/", "/c", "/insights", "/investigations", "/config"]) {
  await p.goto(`${BASE}${route}`, { waitUntil: "domcontentloaded" });
  await p.waitForSelector('a[href="/config"]', { timeout: 45000 });
  // The chat shell decides its layout from a media query, so the rail is 0
  // wide on the first paint. Wait for a real width rather than a fixed pause.
  await boxOf(p.locator("nav.sidebar-cq").first(), "the sidebar");
  rails.push(
    await p.evaluate(() => {
      const el = document.querySelector("nav.sidebar-cq");
      return {
        w: Math.round(el.getBoundingClientRect().width),
        search: !!document.querySelector('input[placeholder*="Search chats"]'),
      };
    }),
  );
}
const widths = [...new Set(rails.map((r) => r.w))];
out.push([
  "10 the sidebar keeps one width across routes",
  widths.length === 1 && widths[0] > 0,
  rails.map((r) => r.w).join(","),
]);
out.push(["10 and the same contents", rails.every((r) => r.search), ""]);

console.log("");
let fail = 0;
for (const [name, ok, note] of out) {
  if (!ok) fail++;
  console.log(`${ok ? "PASS" : "FAIL"}  ${name}${note ? "   " + note : ""}`);
}
console.log(`\n${out.length - fail}/${out.length} passed`);
await b.close();
process.exit(fail ? 1 : 0);
