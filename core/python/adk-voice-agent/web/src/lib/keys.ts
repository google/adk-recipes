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
 * React keys for list items that have no id of their own: each item's text,
 * numbered on repeats (`1|Done`, `2|Done`). Unlike an array index, a key stays
 * with its item when the list grows or shifts.
 */
export function keyedByText<T>(
  items: readonly T[],
  text: (item: T) => string = String,
): Array<{ key: string; item: T }> {
  const seen = new Map<string, number>();
  return items.map((item) => {
    const base = text(item);
    const n = (seen.get(base) ?? 0) + 1;
    seen.set(base, n);
    return { key: `${n}|${base}`, item };
  });
}
