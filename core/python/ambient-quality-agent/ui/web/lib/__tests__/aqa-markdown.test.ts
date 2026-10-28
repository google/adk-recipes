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
import { stripMarkdownForSummary } from "../aqa-markdown";

describe("stripMarkdownForSummary", () => {
  it.each([
    [
      "**Session review:** 3 sessions evaluated.\n\n1 failed.",
      "Session review: 3 sessions evaluated. 1 failed.",
    ],
    [
      "- **Running code:**\n```python\nimport pandas as pd\n```",
      "Running code:",
    ],
    ["Intro\n```python\nx = 1", "Intro"],
    ["Before\n```\ncode\n```\nAfter", "Before After"],
    ["*italic* start", "italic start"],
    ["**bold with *em* inside** rest", "bold with em inside rest"],
    ["_x_", "x"],
    [
      "**Session review:** _Skipped (zero budget)._",
      "Session review: Skipped (zero budget).",
    ],
    ["-5 sessions failed", "-5 sessions failed"],
    ["#3 ranked cluster", "#3 ranked cluster"],
    ["*", "*"],
    ["2 * 3 * 4", "2 * 3 * 4"],
    ["call list_sessions_by_id now", "call list_sessions_by_id now"],
    ["# Heading\n- item\n* star\n> quote", "Heading item star quote"],
    ["Intro\n\n---\n\nMore", "Intro More"],
    ["***", ""],
    ["Run `pytest -q` again", "Run pytest -q again"],
  ])("strips %j to %j", (input, expected) => {
    expect(stripMarkdownForSummary(input)).toBe(expected);
  });
});
