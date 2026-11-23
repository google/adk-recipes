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

// Checks the built shell after `npm run build`: its first <script> must be
// index.html's viewer storage script, classic and unchanged, so it still runs
// before anything else the build put in the page. The vitest suite checks
// only the source index.html, which a Vite plugin or an HTML transform can
// reorder or rewrite on the way to the bundle.
//
// Usage: node scripts/check-built-shell.mjs [built index.html]

import { readFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const WEB = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const SCRIPT = /<script\b([^>]*)>([\s\S]*?)<\/script>/;

const builtPath =
  process.argv[2] ?? path.resolve(WEB, "../static_v2/index.html");
const source = readFileSync(path.resolve(WEB, "index.html"), "utf8");
const built = readFileSync(builtPath, "utf8");

const [, , storageScript] = source.match(SCRIPT);
const [, attributes, firstBuilt] = built.match(SCRIPT) ?? [];

if (
  !storageScript.includes('meta[name="aqua-viewer"]') ||
  firstBuilt !== storageScript ||
  /\b(type|src)\s*=/.test(attributes)
) {
  console.error(
    `${builtPath}: the first <script> is not index.html's viewer storage ` +
      "script as a classic inline script; in viewer mode something could " +
      "read the browser's real storage before it runs.",
  );
  process.exit(1);
}
console.log(`${builtPath}: the viewer storage script runs first.`);
