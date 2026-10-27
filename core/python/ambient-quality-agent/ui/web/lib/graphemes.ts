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
 * Returns the longest prefix of `text` made of whole user-perceived
 * characters whose UTF-16 length is at most `maxLength`.
 *
 * `String.slice` counts UTF-16 units, so it can split a surrogate pair into
 * a broken glyph or cut a ZWJ emoji or a letter's combining accents.
 */
export function sliceGraphemes(text: string, maxLength: number): string {
  if (text.length <= maxLength) return text;
  const segments =
    typeof Intl.Segmenter === "function"
      ? Array.from(new Intl.Segmenter().segment(text), (s) => s.segment)
      : Array.from(text); // code points: never splits a surrogate pair
  let out = "";
  for (const segment of segments) {
    if (out.length + segment.length > maxLength) break;
    out += segment;
  }
  return out;
}

/**
 * Returns the value and caret a length-capped text field should show after an
 * edit, given its value before the edit (`prev`), the value the browser
 * produced (`next`) and the caret in `next`.
 *
 * An edit that leaves at most `maxLength` UTF-16 units passes through
 * unchanged. Otherwise the edit's deletions are kept and only as much of the
 * inserted text as fits is inserted, in whole user-perceived characters, with
 * the caret after it. The text already in the field is never cut, so typing
 * into a full field changes nothing, where truncating `next` would drop the
 * field's last character instead.
 */
export function capEdit(
  prev: string,
  next: string,
  caret: number,
  maxLength: number,
): { value: string; caret: number } {
  if (next.length <= maxLength) return { value: next, caret };
  // An edit replaces one range of `prev`, and the text after the caret is
  // `prev`'s own tail; what precedes the edit is their common prefix.
  const tail = next.length - caret;
  const limit = Math.min(caret, prev.length - tail);
  let start = 0;
  while (start < limit && prev[start] === next[start]) start++;
  const kept = prev.slice(0, start) + prev.slice(prev.length - tail);
  const inserted = sliceGraphemes(
    next.slice(start, caret),
    Math.max(0, maxLength - kept.length),
  );
  return {
    value: prev.slice(0, start) + inserted + prev.slice(prev.length - tail),
    caret: start + inserted.length,
  };
}
