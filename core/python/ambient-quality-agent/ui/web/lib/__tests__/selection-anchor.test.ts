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
import { anchorLabel, resolveAnchor } from "../selection-anchor";

describe("resolveAnchor", () => {
  it("reports the offsets and text of the selection, unmodified", () => {
    const text = "alpha\nbravo\ncharlie\n";
    expect(resolveAnchor(text, 6, 11)).toEqual({
      ok: true,
      anchor: {
        start: 6,
        end: 11,
        snippet: "bravo",
        startLine: 2,
        endLine: 2,
      },
    });
  });

  it("does not widen a selection whose text repeats", () => {
    // The whole point of offsets: three identical lines stay distinguishable,
    // and the user gets back exactly what they highlighted.
    const text = "b\nb\nb\n";
    const r = resolveAnchor(text, 2, 3);
    expect(r).toEqual({
      ok: true,
      anchor: { start: 2, end: 3, snippet: "b", startLine: 2, endLine: 2 },
    });
  });

  it("handles a selection inside a line", () => {
    const r = resolveAnchor("# dogs\n", 2, 6);
    expect(r.ok && r.anchor).toEqual({
      start: 2,
      end: 6,
      snippet: "dogs",
      startLine: 1,
      endLine: 1,
    });
  });

  it("reports 1-based inclusive line numbers for a multi-line selection", () => {
    const text = "one\ntwo\nthree\nfour\n";
    const r = resolveAnchor(text, 4, 13);
    expect(r.ok && r.anchor.snippet).toBe("two\nthree");
    expect(r.ok && r.anchor.startLine).toBe(2);
    expect(r.ok && r.anchor.endLine).toBe(3);
  });

  it("does not claim the next line when the selection ends on a newline", () => {
    const r = resolveAnchor("one\ntwo\n", 0, 4);
    expect(r.ok && r.anchor.endLine).toBe(1);
  });

  it("rejects an empty, collapsed, inverted or blank selection", () => {
    expect(resolveAnchor("abc", 1, 1)).toEqual({ ok: false });
    expect(resolveAnchor("abc", 2, 1)).toEqual({ ok: false });
    expect(resolveAnchor("   \n  ", 0, 5)).toEqual({ ok: false });
  });

  it("accepts a selection of the whole file, however large", () => {
    // No cap: offsets stay exact regardless of span length.
    const text = `${Array(500).fill("x").join("\n")}\n`;
    const r = resolveAnchor(text, 0, text.length);
    expect(r.ok).toBe(true);
    // The trailing newline belongs to line 500, so that is the last line.
    expect(r.ok && r.anchor.endLine).toBe(500);
  });
});

describe("anchorLabel", () => {
  it("returns short text unchanged, on one line", () => {
    expect(anchorLabel("hello world")).toBe("hello world");
    expect(anchorLabel("one\ntwo")).toBe("one two");
  });

  it("collapses runs of whitespace", () => {
    expect(anchorLabel("a   \n\n  b")).toBe("a b");
  });

  it("truncates in the middle so both ends stay visible", () => {
    const out = anchorLabel(`${"H".repeat(60)} ${"T".repeat(60)}`);
    expect(out.startsWith("H".repeat(40))).toBe(true);
    expect(out.endsWith("T".repeat(40))).toBe(true);
    expect(out.length).toBe(83); // 40 + " … " + 40
  });
});

describe("offsets on the wire", () => {
  it("counts code points, not UTF-16 units", () => {
    // The server slices code points; the DOM counts UTF-16 units, so an
    // astral character earlier in the file shifts every later offset by one.
    const text = "🐈 cat\nbravo\n";
    const utf16Start = text.indexOf("bravo");
    const r = resolveAnchor(text, utf16Start, utf16Start + 5);
    expect(r.ok).toBe(true);
    if (!r.ok) return;
    expect(r.anchor.snippet).toBe("bravo");
    // [..."🐈 cat\n"].length === 6, not 7.
    expect(r.anchor.start).toBe(6);
    expect(r.anchor.end).toBe(11);
    // And the server's slice of those offsets is the selection.
    expect([...text].slice(r.anchor.start, r.anchor.end).join("")).toBe(
      "bravo",
    );
  });

  it("is unchanged for plain ASCII", () => {
    const r = resolveAnchor("alpha\nbravo\n", 6, 11);
    expect(r.ok && r.anchor.start).toBe(6);
    expect(r.ok && r.anchor.end).toBe(11);
  });
});
