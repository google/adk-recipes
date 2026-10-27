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
import {
  applyMarkdownCommand,
  type MarkdownCommand,
} from "../markdown-commands";

/**
 * `|` marks a caret, `[...]` marks a selection. Keeps each case readable as
 * the before/after a user would actually see.
 */
function parse(spec: string) {
  const selMatch = /\[([\s\S]*?)\]/.exec(spec);
  if (selMatch) {
    const start = selMatch.index;
    const inner = selMatch[1];
    return {
      text:
        spec.slice(0, start) + inner + spec.slice(start + selMatch[0].length),
      start,
      end: start + inner.length,
    };
  }
  const caret = spec.indexOf("|");
  return {
    text: spec.replace("|", ""),
    start: caret,
    end: caret,
  };
}

function render(state: { text: string; start: number; end: number }) {
  if (state.start === state.end) {
    return (
      state.text.slice(0, state.start) + "|" + state.text.slice(state.start)
    );
  }
  return (
    state.text.slice(0, state.start) +
    "[" +
    state.text.slice(state.start, state.end) +
    "]" +
    state.text.slice(state.end)
  );
}

function run(spec: string, cmd: MarkdownCommand) {
  return render(applyMarkdownCommand(parse(spec), cmd));
}

describe("inline wrapping", () => {
  it("wraps a selection and keeps it selected", () => {
    expect(run("say [hello] there", "bold")).toBe("say **[hello]** there");
    expect(run("say [hello] there", "italic")).toBe("say *[hello]* there");
    expect(run("say [hello] there", "strikethrough")).toBe(
      "say ~~[hello]~~ there",
    );
    expect(run("say [hello] there", "code")).toBe("say `[hello]` there");
  });

  it("inserts a selected placeholder when nothing is selected", () => {
    // Typing immediately replaces it, so the button is usable with no
    // selection at all.
    expect(run("say | there", "bold")).toBe("say **[bold text]** there");
  });

  it("unwraps when the markers are inside the selection", () => {
    expect(run("say [**hello**] there", "bold")).toBe("say [hello] there");
  });

  it("unwraps when the markers sit just outside the selection", () => {
    // Double-clicking a bold word selects the word, not the asterisks.
    expect(run("say **[hello]** there", "bold")).toBe("say [hello] there");
  });

  it("does not mistake bold for italic when toggling", () => {
    expect(run("say [**hello**] there", "italic")).toBe(
      "say *[**hello**]* there",
    );
  });
});

describe("line prefixes", () => {
  it("adds a heading to the caret's line", () => {
    expect(run("hel|lo\nworld", "h1")).toBe("[# hello]\nworld");
  });

  it("replaces one heading level with another rather than stacking", () => {
    expect(run("# hel|lo", "h2")).toBe("[## hello]");
  });

  it("toggles a heading off", () => {
    expect(run("## hel|lo", "h2")).toBe("[hello]");
  });

  it("prefixes every line of a multi-line selection", () => {
    expect(run("[one\ntwo]", "bullet")).toBe("[- one\n- two]");
  });

  it("toggles a list off only when every line has it", () => {
    expect(run("[- one\n- two]", "bullet")).toBe("[one\ntwo]");
    // Mixed: fill in the gap rather than strip what's there.
    expect(run("[- one\ntwo]", "bullet")).toBe("[- one\n- two]");
  });

  it("numbers an ordered list from one", () => {
    expect(run("[one\ntwo\nthree]", "ordered")).toBe(
      "[1. one\n2. two\n3. three]",
    );
  });

  it("converts a bullet list to an ordered list", () => {
    expect(run("[- one\n- two]", "ordered")).toBe("[1. one\n2. two]");
  });

  it("supports task lists and quotes", () => {
    expect(run("[one]", "task")).toBe("[- [ ] one]");
    expect(run("[one]", "quote")).toBe("[> one]");
  });
});

describe("blocks", () => {
  it("puts the caret on the url of a new link", () => {
    expect(run("see [here] now", "link")).toBe("see [here]([url]) now");
  });

  it("uses a placeholder label when nothing is selected", () => {
    expect(run("see | now", "link")).toBe("see [text]([url]) now");
  });

  it("fences a selection as a code block", () => {
    expect(run("[x = 1]", "codeblock")).toBe("```\n[x = 1]\n```\n");
  });

  it("inserts a table with example cells and the first header selected", () => {
    // Filled cells give something to click into; an empty row is a blank
    // strip with no visible target.
    expect(run("|", "table")).toBe(
      "| [Column] | Column |\n| --- | --- |\n| Cell | Cell |\n| Cell | Cell |\n",
    );
  });
});

describe("a selection that includes its trailing newline", () => {
  it("does not prefix the line after it", () => {
    // Triple-click and shift+Down both produce this shape.
    expect(run("[one\n]two\nthree", "bullet")).toBe("[- one]\ntwo\nthree");
  });

  it("still covers every line of a multi-line selection", () => {
    expect(run("[one\ntwo\n]three", "bullet")).toBe("[- one\n- two]\nthree");
  });
});
