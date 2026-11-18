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

import { describe, expect, it } from "vitest";
import { keyedByText } from "./keys";

describe("keyedByText", () => {
  it("keys each item by its text, numbering repeats", () => {
    expect(keyedByText(["a", "b", "a"]).map((k) => k.key)).toEqual([
      "1|a",
      "1|b",
      "2|a",
    ]);
  });

  it("keeps earlier keys when the list grows", () => {
    const before = keyedByText(["Done", "Tests pass"]).map((k) => k.key);
    const after = keyedByText(["Done", "Tests pass", "Done"]).map((k) => k.key);
    expect(after.slice(0, 2)).toEqual(before);
    expect(new Set(after).size).toBe(after.length);
  });

  it("uses the text function for objects", () => {
    const rows = [{ id: "x" }, { id: "y" }];
    expect(keyedByText(rows, (r) => r.id)).toEqual([
      { key: "1|x", item: rows[0] },
      { key: "1|y", item: rows[1] },
    ]);
  });
});
