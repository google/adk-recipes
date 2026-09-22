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

/**
 * Text transforms behind the markdown toolbar.
 *
 * Pure: text plus a selection in, text plus a new selection out. The editor
 * only applies the result and restores the caret, so every rule here — what
 * toggles off, what a placeholder is, where the caret lands — is testable
 * without a DOM.
 */

export interface EditState {
  text: string;
  start: number;
  end: number;
}

export type MarkdownCommand =
  | "bold"
  | "italic"
  | "strikethrough"
  | "code"
  | "h1"
  | "h2"
  | "h3"
  | "bullet"
  | "ordered"
  | "task"
  | "quote"
  | "codeblock"
  | "link"
  | "table";

const WRAPS: Record<string, { marker: string; placeholder: string }> = {
  bold: { marker: "**", placeholder: "bold text" },
  italic: { marker: "*", placeholder: "italic text" },
  strikethrough: { marker: "~~", placeholder: "struck text" },
  code: { marker: "`", placeholder: "code" },
};

const LINE_PREFIXES: Record<string, string> = {
  h1: "# ",
  h2: "## ",
  h3: "### ",
  bullet: "- ",
  task: "- [ ] ",
  quote: "> ",
};

// Any prefix this family owns, so applying one replaces another rather than
// stacking (`## - # heading`).
const HEADING_RE = /^#{1,6} /;
const BULLET_RE = /^- (\[[ xX]\] )?/;
const ORDERED_RE = /^\d+\. /;
const QUOTE_RE = /^> /;

function stripLinePrefix(line: string): string {
  return line
    .replace(HEADING_RE, "")
    .replace(BULLET_RE, "")
    .replace(ORDERED_RE, "")
    .replace(QUOTE_RE, "");
}

/** Expand a selection to cover the whole lines it touches. */
function lineSpan(text: string, start: number, end: number) {
  const from = text.lastIndexOf("\n", start - 1) + 1;
  // A selection that already swallowed its trailing newline (triple-click,
  // shift+Down) must not also take the line after it.
  if (end > start && text[end - 1] === "\n") return { from, to: end - 1 };
  let to = text.indexOf("\n", end);
  if (to === -1) to = text.length;
  return { from, to };
}

function replace(
  state: EditState,
  from: number,
  to: number,
  body: string,
  selStart: number,
  selEnd: number,
): EditState {
  return {
    text: state.text.slice(0, from) + body + state.text.slice(to),
    start: selStart,
    end: selEnd,
  };
}

/**
 * Length of the run of `ch` immediately before / at index `i`.
 *
 * `*` is both the italic and the bold marker, so a match only belongs to this
 * style when the run is exactly this wide — otherwise italic would strip one
 * asterisk off `**bold**` and quietly turn it into italic.
 */
function runBefore(text: string, i: number, ch: string): number {
  let n = 0;
  while (i - n - 1 >= 0 && text[i - n - 1] === ch) n++;
  return n;
}

function runAfter(text: string, i: number, ch: string): number {
  let n = 0;
  while (i + n < text.length && text[i + n] === ch) n++;
  return n;
}

function applyWrap(state: EditState, cmd: keyof typeof WRAPS): EditState {
  const { marker, placeholder } = WRAPS[cmd];
  const { text, start, end } = state;
  const selected = text.slice(start, end);
  const ch = marker[0];
  const width = marker.length;

  // Already wrapped, inside the selection — unwrap.
  if (
    selected.length >= width * 2 &&
    runAfter(selected, 0, ch) === width &&
    runBefore(selected, selected.length, ch) === width
  ) {
    const inner = selected.slice(width, selected.length - width);
    return replace(state, start, end, inner, start, start + inner.length);
  }

  // Already wrapped, just outside the selection — unwrap those too, so
  // double-clicking the word itself still toggles off.
  if (
    selected &&
    runBefore(text, start, ch) === width &&
    runAfter(text, end, ch) === width
  ) {
    const from = start - width;
    return replace(
      state,
      from,
      end + width,
      selected,
      from,
      from + selected.length,
    );
  }

  const body = selected || placeholder;
  const inner = start + width;
  return replace(
    state,
    start,
    end,
    `${marker}${body}${marker}`,
    inner,
    inner + body.length,
  );
}

function applyLinePrefix(state: EditState, prefix: string): EditState {
  const { text, start, end } = state;
  const { from, to } = lineSpan(text, start, end);
  const lines = text.slice(from, to).split("\n");
  // Toggle off only when every line already carries exactly this prefix.
  const allHave = lines.every((l) => l.startsWith(prefix));
  const next = lines
    .map((l) =>
      allHave ? l.slice(prefix.length) : prefix + stripLinePrefix(l),
    )
    .join("\n");
  return replace(state, from, to, next, from, from + next.length);
}

function applyOrdered(state: EditState): EditState {
  const { text, start, end } = state;
  const { from, to } = lineSpan(text, start, end);
  const lines = text.slice(from, to).split("\n");
  const allHave = lines.every((l) => ORDERED_RE.test(l));
  const next = lines
    .map((l, i) =>
      allHave ? l.replace(ORDERED_RE, "") : `${i + 1}. ${stripLinePrefix(l)}`,
    )
    .join("\n");
  return replace(state, from, to, next, from, from + next.length);
}

function applyLink(state: EditState): EditState {
  const { text, start, end } = state;
  const label = text.slice(start, end) || "text";
  const body = `[${label}](url)`;
  // Land on `url` — the label is either already right or easy to retype.
  const urlAt = start + body.indexOf("(") + 1;
  return replace(state, start, end, body, urlAt, urlAt + 3);
}

/**
 * Replace the selection with a block, breaking onto a new line first when the
 * caret is mid-line so the fence or table starts where markdown expects.
 */
function insertBlock(
  state: EditState,
  block: string,
  selectFrom: number,
  selectLength: number,
): EditState {
  const { text, start, end } = state;
  const atLineStart = start === 0 || text[start - 1] === "\n";
  const lead = atLineStart ? "" : "\n";
  const at = start + lead.length + selectFrom;
  return replace(state, start, end, lead + block, at, at + selectLength);
}

function applyCodeBlock(state: EditState): EditState {
  const selected = state.text.slice(state.start, state.end);
  const body = selected || "code";
  const block = `\`\`\`\n${body}\n\`\`\`\n`;
  return insertBlock(state, block, 4, body.length);
}

function applyTable(state: EditState): EditState {
  // Filled cells rather than empty ones: an empty row renders as a blank
  // strip, so there is nothing to aim at when replacing the contents.
  const block =
    "| Column | Column |\n| --- | --- |\n| Cell | Cell |\n| Cell | Cell |\n";
  return insertBlock(state, block, 2, 6);
}

export function applyMarkdownCommand(
  state: EditState,
  cmd: MarkdownCommand,
): EditState {
  if (cmd in WRAPS) return applyWrap(state, cmd as keyof typeof WRAPS);
  if (cmd in LINE_PREFIXES) return applyLinePrefix(state, LINE_PREFIXES[cmd]);
  if (cmd === "ordered") return applyOrdered(state);
  if (cmd === "link") return applyLink(state);
  if (cmd === "codeblock") return applyCodeBlock(state);
  return applyTable(state);
}
