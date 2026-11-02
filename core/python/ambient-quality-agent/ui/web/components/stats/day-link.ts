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
 * How a per-day chart column that opens that day's list looks when it is one.
 *
 * The ring is inset because both charts sit in `overflow-hidden` boxes, which
 * clip an outer ring; the columns pad their content off its edge.
 * Hover tints the column behind the bars rather than fading them, so the bars
 * keep their contrast; the tint is of the foreground, since `muted` and
 * `accent` sit within a few percent of the page background in both themes.
 */
export const DAY_LINK =
  "rounded-sm transition-colors hover:bg-foreground/10 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-inset";

/**
 * The narrowest a row of `days` linked columns may get: 24px (1.5rem) each,
 * the WCAG 2.5.8 minimum target. Set on the row rather than on each column,
 * which keep `min-w-0 flex-1` so they share the row evenly (#311); the row
 * sits in an `overflow-x-auto` box that scrolls once its space runs out,
 * which at the default rail width happens below a viewport of about 470px.
 */
export function linkedRowFloor(days: number): { minWidth: string } {
  return { minWidth: `${days * 1.5}rem` };
}
