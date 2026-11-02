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
import { cardChipLabel, outputName } from "./outputs";

describe("outputName", () => {
  it("uses the title, else names the kind", () => {
    expect(outputName({ kind: "pr", title: "Fix login", url: "u" })).toBe(
      "Fix login",
    );
    expect(outputName({ kind: "doc", title: " ", url: "u" })).toBe(
      "Google Doc",
    );
    expect(outputName({ kind: "draft", title: "", url: "u" })).toBe(
      "Gmail draft",
    );
  });
});

describe("cardChipLabel", () => {
  it("counts the card's items by kind", () => {
    expect(cardChipLabel({ kind: "events", items: [{ id: "1" }] })).toBe(
      "1 event",
    );
    expect(
      cardChipLabel({
        kind: "threads",
        items: [{ id: "1" }, { id: "2" }] as never,
      }),
    ).toBe("2 emails");
  });
});
