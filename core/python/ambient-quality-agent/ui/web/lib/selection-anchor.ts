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
 * Describe a highlighted span as a range in the raw file.
 *
 * The span is identified by character offsets, not by its text. Content-based
 * anchors cannot survive a file where the same words repeat, and they break
 * outright in the rendered preview, where a selection reads `important` while
 * the source says `**important**`. `patch` verifies the text at the offsets
 * before writing, so position gives precision and content gives safety.
 *
 * This is the seam between a selection surface and the wire payload. The
 * Source textarea resolves offsets directly; a future preview editor resolves
 * them from mdast node positions, which carry offsets into the same raw
 * source. Both produce a `SelectionAnchor`, and nothing downstream changes.
 */

export interface SelectionAnchor {
  /**
   * Offsets into the raw file, as **code points** — `start` inclusive, `end`
   * exclusive. The DOM counts UTF-16 units, but the server slices code
   * points, so one emoji earlier in the file would shift every offset by one.
   */
  start: number;
  end: number;
  /** The text at that range, used by the backend to verify before writing. */
  snippet: string;
  /** 1-based, inclusive. Display only. */
  startLine: number;
  /** 1-based, inclusive. Display only. */
  endLine: number;
}

export type AnchorResult =
  | { ok: true; anchor: SelectionAnchor }
  | { ok: false };

/**
 * Convert a UTF-16 offset (what the DOM reports) to a code-point index (what
 * Python's slicing uses). Equal for the ASCII case, off by one per astral
 * character otherwise — an emoji anywhere earlier in the document.
 */
function codePointIndex(text: string, utf16Offset: number): number {
  return [...text.slice(0, utf16Offset)].length;
}

/** Index of the line (0-based) containing character offset `pos`. */
function lineAt(lineStarts: number[], pos: number): number {
  let lo = 0;
  let hi = lineStarts.length - 1;
  while (lo < hi) {
    const mid = Math.ceil((lo + hi) / 2);
    if (lineStarts[mid] <= pos) lo = mid;
    else hi = mid - 1;
  }
  return lo;
}

export function resolveAnchor(
  text: string,
  start: number,
  end: number,
): AnchorResult {
  if (end <= start) return { ok: false };
  const snippet = text.slice(start, end);
  if (!snippet.trim()) return { ok: false };

  const lineStarts: number[] = [];
  let acc = 0;
  for (const l of text.split("\n")) {
    lineStarts.push(acc);
    acc += l.length + 1;
  }

  return {
    ok: true,
    anchor: {
      start: codePointIndex(text, start),
      end: codePointIndex(text, end),
      snippet,
      startLine: lineAt(lineStarts, start) + 1,
      // `end` is exclusive; step back one char so a selection ending exactly
      // at a newline doesn't claim the following line.
      endLine: lineAt(lineStarts, Math.max(start, end - 1)) + 1,
    },
  };
}

const LABEL_SIDE = 40;

/**
 * One-line chip text. Truncates in the *middle*: with a range, both ends need
 * confirming — a tail-truncated label tells you nothing about where the
 * selection stops.
 */
export function anchorLabel(snippet: string): string {
  const flat = snippet.replace(/\s+/g, " ").trim();
  if (flat.length <= LABEL_SIDE * 2 + 3) return flat;
  return `${flat.slice(0, LABEL_SIDE)} … ${flat.slice(-LABEL_SIDE)}`;
}
