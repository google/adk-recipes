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

import { type Dirent, readFileSync, readdirSync } from "node:fs";
import path from "node:path";
import { describe, expect, it } from "vitest";
import { newId } from "@/lib/new-id";

const UUID_V4 =
  /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;

const WEB_ROOT = path.resolve(__dirname, "../..");
const SOURCE_DIRS = ["lib", "components", "app", "src", "routes"];
const SKIP_DIRS = new Set(["node_modules", "dist", "__tests__"]);

function sourceFiles(dir: string): string[] {
  let entries: Dirent[];
  try {
    entries = readdirSync(dir, { withFileTypes: true });
  } catch {
    return []; // A directory this tree does not have; not this test's business.
  }
  return entries.flatMap((e) => {
    const full = path.join(dir, e.name);
    if (e.isDirectory()) return SKIP_DIRS.has(e.name) ? [] : sourceFiles(full);
    return /\.tsx?$/.test(e.name) ? [full] : [];
  });
}

describe("newId", () => {
  it("mints distinct v4 uuids", () => {
    const ids = new Set(Array.from({ length: 100 }, () => newId()));
    expect(ids.size).toBe(100);
    for (const id of ids) expect(id).toMatch(UUID_V4);
  });

  it("works where crypto.randomUUID does not", () => {
    // The whole point. `randomUUID` is gated on a secure context and is absent
    // on a plain-http origin that is not literally localhost; `getRandomValues`
    // is not gated, and is what `uuid` reaches for.
    const original = Object.getOwnPropertyDescriptor(crypto, "randomUUID");
    Object.defineProperty(crypto, "randomUUID", {
      value: undefined,
      configurable: true,
    });
    try {
      expect(newId()).toMatch(UUID_V4);
    } finally {
      if (original) Object.defineProperty(crypto, "randomUUID", original);
    }
  });

  // A source scan rather than a runtime assertion, because there is no runtime
  // to assert in: every developer origin is localhost, where `randomUUID`
  // works, so a reintroduced call passes every test and every manual check and
  // only breaks for whoever opens the dashboard by hostname or IP.
  it("is the only id generator in the tree", () => {
    const offenders = SOURCE_DIRS.flatMap((d) =>
      sourceFiles(path.join(WEB_ROOT, d)),
    )
      .filter((f) => readFileSync(f, "utf8").includes("crypto.randomUUID"))
      .map((f) => path.relative(WEB_ROOT, f))
      // The module whose whole subject is why not to call it.
      .filter((f) => f !== path.join("lib", "new-id.ts"));

    expect(offenders, "use newId() from @/lib/new-id instead").toEqual([]);
  });
});
