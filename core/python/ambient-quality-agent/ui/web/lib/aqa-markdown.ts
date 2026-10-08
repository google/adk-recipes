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

export type Inline =
  | { t: "text"; v: string }
  | { t: "code"; v: string }
  | { t: "strong"; v: Inline[] };

export type Block =
  | { t: "p"; body: Inline[] }
  | { t: "fence"; lang: string; code: string }
  | { t: "list"; items: Inline[][]; fence?: { lang: string; code: string } };

const INLINE_MARKER = /\*\*([^*]+)\*\*|`([^`]+)`/;

export function inlines(src: string): Inline[] {
  const out: Inline[] = [];
  let rest = src;
  for (;;) {
    const m = INLINE_MARKER.exec(rest);
    if (!m) {
      if (rest) out.push({ t: "text", v: rest });
      return out;
    }
    if (m.index > 0) out.push({ t: "text", v: rest.slice(0, m.index) });
    if (m[1] !== undefined) out.push({ t: "strong", v: inlines(m[1]) });
    else out.push({ t: "code", v: m[2] });
    rest = rest.slice(m.index + m[0].length);
  }
}

const FENCE_OPEN = /^```(\w*)\s*$/;
const FENCE_CLOSE = /^```\s*$/;
const LIST_ITEM = /^\s*-\s+/;

function consumeFence(lines: string[], openLine: string, i: number) {
  const fenceOpen = FENCE_OPEN.exec(openLine) as RegExpExecArray;
  const body: string[] = [];
  let j = i + 1;
  while (j < lines.length && !FENCE_CLOSE.test(lines[j])) {
    body.push(lines[j]);
    j++;
  }
  j++;
  return {
    fence: { lang: fenceOpen[1] || "text", code: body.join("\n") },
    next: j,
  };
}

export function blocks(src: string): Block[] {
  const lines = src.split("\n");
  const out: Block[] = [];
  let i = 0;

  while (i < lines.length) {
    if (FENCE_OPEN.test(lines[i])) {
      const { fence, next } = consumeFence(lines, lines[i], i);
      out.push({ t: "fence", ...fence });
      i = next;
      continue;
    }

    if (LIST_ITEM.test(lines[i])) {
      const items: Inline[][] = [];
      while (i < lines.length && LIST_ITEM.test(lines[i])) {
        items.push(inlines(lines[i].replace(LIST_ITEM, "")));
        i++;
      }
      if (i < lines.length && FENCE_OPEN.test(lines[i])) {
        const { fence, next } = consumeFence(lines, lines[i], i);
        out.push({ t: "list", items, fence });
        i = next;
        continue;
      }
      out.push({ t: "list", items });
      continue;
    }

    if (lines[i].trim() === "") {
      i++;
      continue;
    }

    const paraLines: string[] = [];
    while (
      i < lines.length &&
      lines[i].trim() !== "" &&
      !FENCE_OPEN.test(lines[i]) &&
      !LIST_ITEM.test(lines[i])
    ) {
      paraLines.push(lines[i]);
      i++;
    }
    out.push({ t: "p", body: inlines(paraLines.join("\n")) });
  }

  return out;
}

const BOLD_PREFIX = /^\*\*([^*]+)\*\*(?::\s*|\s*)(.*)$/s;

export function headingOf(
  text: string,
): { heading: string; rest: string } | null {
  const m = BOLD_PREFIX.exec(text.trim());
  if (!m) return null;
  return { heading: m[1], rest: m[2] };
}

// A fence runs to its closing line, or to the end of the text when the
// closing line is missing.
const FENCED_BLOCK = /^[ \t]*```.*$[\s\S]*?(?:^[ \t]*```[ \t]*$|(?![\s\S]))/gm;
const HORIZONTAL_RULE = /^[ \t]*([-*_])(?:[ \t]*\1){2,}[ \t]*$/gm;
// Requiring whitespace after the marker keeps "-5 sessions" and "#3 ranked".
const LINE_MARKER = /^[ \t]*(?:#{1,6}|[*>-])[ \t]+/gm;
// Emphasis must hug its text, so "2 * 3 * 4" keeps its asterisks.
const BOLD = /\*\*(?!\s)(.+?)(?<!\s)\*\*/g;
const ITALIC_STAR = /\*(?!\s)([^*\n]+?)(?<!\s)\*/g;
// Underscores inside a word, as in snake_case names, are not emphasis.
const ITALIC_UNDERSCORE = /(?<!\w)_(?!\s)([^_\n]+?)(?<!\s)_(?!\w)/g;
const CODE_SPAN = /`([^`]+)`/g;

/**
 * Reduces markdown to one line of plain text for a ledger row summary.
 * Fenced code blocks are dropped, since a one-line row has no room for them.
 */
export function stripMarkdownForSummary(text: string): string {
  return text
    .replace(FENCED_BLOCK, "")
    .replace(HORIZONTAL_RULE, "")
    .replace(LINE_MARKER, "")
    .replace(BOLD, "$1")
    .replace(ITALIC_STAR, "$1")
    .replace(ITALIC_UNDERSCORE, "$1")
    .replace(CODE_SPAN, "$1")
    .replace(/\s+/g, " ")
    .trim();
}
