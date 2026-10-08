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

import { capEdit, sliceGraphemes } from "@/lib/graphemes";
import {
  listConversations,
  rememberConversation,
  resetConversationsForTest,
} from "@/lib/aqua-conversations";

const FAMILY = "👨‍👩‍👧‍👦"; // one grapheme, 11 UTF-16 units

describe("sliceGraphemes", () => {
  it("returns short text unchanged", () => {
    expect(sliceGraphemes("hello", 80)).toBe("hello");
  });

  it("cuts plain text at the limit", () => {
    expect(sliceGraphemes("abcdef", 3)).toBe("abc");
  });

  it("drops a surrogate pair that would straddle the limit", () => {
    expect(sliceGraphemes("ab😀", 3)).toBe("ab");
  });

  it("drops a whole ZWJ sequence rather than part of it", () => {
    expect(sliceGraphemes(`${FAMILY}${FAMILY}`, 15)).toBe(FAMILY);
  });

  it("keeps a base letter together with its combining marks", () => {
    expect(sliceGraphemes("ae\u0301", 2)).toBe("a");
  });
});

describe("capEdit", () => {
  it("passes an edit that fits straight through", () => {
    expect(capEdit("abc", "abXc", 3, 5)).toEqual({ value: "abXc", caret: 3 });
  });

  it("refuses a keystroke into a full field, keeping its text and caret", () => {
    expect(capEdit("abcde", "abXcde", 3, 5)).toEqual({
      value: "abcde",
      caret: 2,
    });
  });

  it("inserts as much of a paste as fits, in whole graphemes", () => {
    // Room for four units: "b😀c" fits, "d" does not.
    expect(
      capEdit(`${"a".repeat(76)}`, `aab😀cd${"a".repeat(74)}`, 7, 80),
    ).toEqual({
      value: `aab😀c${"a".repeat(74)}`,
      caret: 6,
    });
    // Room for 10 units does not take half of an 11-unit family emoji.
    expect(
      capEdit("a".repeat(70), `${FAMILY}${"a".repeat(70)}`, 11, 80),
    ).toEqual({
      value: "a".repeat(70),
      caret: 0,
    });
  });

  it("replaces a selection with what fits of the text typed over it", () => {
    // "cd" selected in a full field and replaced with three characters.
    expect(capEdit("abcde", "abXYZe", 5, 5)).toEqual({
      value: "abXYe",
      caret: 4,
    });
  });

  it("lets a field that is already too long be shortened", () => {
    expect(capEdit("abcdefg", "abcdfg", 4, 5)).toEqual({
      value: "abcdfg",
      caret: 4,
    });
  });
});

describe("conversation titles from the first message", () => {
  it("does not end on half an emoji", () => {
    resetConversationsForTest();
    rememberConversation("c1", `${"a".repeat(56)}${FAMILY}`);
    expect(listConversations()[0].title).toBe(`${"a".repeat(56)}…`);
  });
});
