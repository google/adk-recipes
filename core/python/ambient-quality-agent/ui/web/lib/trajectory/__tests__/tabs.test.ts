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
import { defaultTabFor, tabsFor } from "../tabs";
import type { TrajectoryRecord } from "../types";

function record(overrides: Partial<TrajectoryRecord>): TrajectoryRecord {
  return {
    id: "r1",
    index: 0,
    kind: "assistant",
    turn: 0,
    agent: "agent",
    text: "hello",
    raw: {},
    ...overrides,
  };
}

describe("tabsFor", () => {
  it("never offers Tools for a record with no call to show", () => {
    const tabs = tabsFor(record({ kind: "assistant", text: "hello" }));
    expect(tabs.map((t) => t.id)).toEqual(["preview", "raw"]);
  });

  it("never offers Preview for a tool record, whose text is always empty", () => {
    const tabs = tabsFor(
      record({ kind: "tool", toolName: "create_ticket", args: {} }),
    );
    expect(tabs.map((t) => t.id)).toEqual(["tools", "raw"]);
  });

  it("defaults a tool record to Tools and everything else to Preview", () => {
    expect(defaultTabFor(record({ kind: "tool", toolName: "x" }))).toBe(
      "tools",
    );
    expect(defaultTabFor(record({ kind: "assistant" }))).toBe("preview");
  });
});
