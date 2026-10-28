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
 * Discriminator on the DataPart a highlighted selection travels in.
 *
 * The payload is sent untyped (no ADK metadata key), so the backend's
 * `unwrap_a2a_datapart_text` sees it and renders the model-facing prose;
 * the chat bubble keys off the same field to draw a quote card instead of
 * the generic JSON dump. Both sides must agree on this string.
 */
export const SELECTION_DATA_KIND = "lha.selection";

export interface SelectionPayload {
  lha_kind: typeof SELECTION_DATA_KIND;
  path: string;
  /** Character offsets into the raw file — what actually identifies the span. */
  start: number;
  end: number;
  /** Display only; the backend never locates by line. */
  start_line: number;
  end_line: number;
  snippet: string;
}

export function isSelectionPayload(v: unknown): v is SelectionPayload {
  if (typeof v !== "object" || v === null) return false;
  const o = v as Record<string, unknown>;
  return (
    o.lha_kind === SELECTION_DATA_KIND &&
    typeof o.path === "string" &&
    typeof o.snippet === "string" &&
    typeof o.start === "number" &&
    typeof o.end === "number" &&
    typeof o.start_line === "number" &&
    typeof o.end_line === "number"
  );
}
