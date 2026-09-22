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

const WORKSPACE_ROOT = "/workspace";

/**
 * Discriminator on the DataPart a highlighted selection travels in.
 *
 * The payload is sent untyped (no ADK metadata key), so the backend's
 * `unwrap_a2a_datapart_text` sees it and renders the model-facing prose;
 * the chat bubble keys off the same field to draw a quote card instead of
 * the generic JSON dump. Both sides must agree on this string.
 */
export const SELECTION_DATA_KIND = "lha.selection";

/**
 * Discriminator on the DataPart a dragged workspace file travels in.
 * Must match _FILEREF_KIND in horizon/a2a/datapart.py.
 */
export const FILEREF_DATA_KIND = "lha.fileref";

/** Drag payload for a workspace file, and the mime it is carried under. */
export const FILEREF_DRAG_MIME = "application/x-lha-workspace-file";

export interface FileRef {
  path: string;
  name: string;
  size?: number;
}

export interface FileRefPayload {
  lha_kind: typeof FILEREF_DATA_KIND;
  paths: string[];
}

export function isFileRefPayload(v: unknown): v is FileRefPayload {
  if (typeof v !== "object" || v === null) return false;
  const o = v as Record<string, unknown>;
  return (
    o.lha_kind === FILEREF_DATA_KIND &&
    Array.isArray(o.paths) &&
    o.paths.every((p) => typeof p === "string")
  );
}

export function parseFileRefDrag(raw: string): FileRef | null {
  try {
    const v = JSON.parse(raw) as Record<string, unknown>;
    if (typeof v.path !== "string" || typeof v.name !== "string") return null;
    return {
      path: v.path,
      name: v.name,
      size: typeof v.size === "number" ? v.size : undefined,
    };
  } catch {
    return null;
  }
}

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

/**
 * Normalise a workspace path to its canonical ``/workspace/...`` form.
 *
 * Tabs are keyed by path, so the sidebar (`/workspace/report.md`) and a
 * markdown link (`report.md`) have to agree or the same file opens twice.
 */
export function normalizeWorkspacePath(raw: string): string | null {
  let rel = raw.trim();
  if (!rel) return null;
  if (rel.startsWith(WORKSPACE_ROOT)) {
    rel = rel.slice(WORKSPACE_ROOT.length);
  } else if (rel.startsWith("/")) {
    // An absolute host path (what write_file echoes back on the local
    // backend) isn't addressable through the workspace API.
    return null;
  }
  rel = rel.replace(/^\.\//, "").replace(/^\/+/, "");
  if (!rel) return null;
  // Traversal is rejected server-side too; bail early so we never render a
  // panel link that can only 400.
  if (rel.split("/").some((seg) => seg === ".." || seg === ".")) return null;
  return `${WORKSPACE_ROOT}/${rel}`;
}

/**
 * Return the workspace path a markdown href points at, or ``null`` when the
 * link belongs to the wider web.
 *
 * Only bare relative hrefs qualify — the model writing ``[plot.html](plot.html)``
 * after generating it. Anything with a scheme, or rooted at ``/``/``#``/``?``,
 * is a real link and is left alone.
 */
// A filename, not a shell line or a sentence: no whitespace, and a short
// extension that starts with a letter (so version numbers like `3.14` and
// `v1.2` don't qualify). Membership in the workspace listing is the real
// gate — this just avoids normalising every backticked token.
const FILENAME_MENTION = /^[^\s`]+\.[A-Za-z][A-Za-z0-9]{0,7}$/;

/**
 * Return the workspace path a backticked mention refers to, or ``null``.
 *
 * Models write ``I created `cats.md``` far more often than a markdown link,
 * so inline code is where most workspace-file references land. Callers must
 * still confirm the file exists before rendering a link.
 */
export function workspacePathFromMention(raw: string): string | null {
  const text = raw.trim();
  if (!FILENAME_MENTION.test(text)) return null;
  // Command-ish and URL-ish tokens share the shape but aren't paths.
  if (/^[a-z][a-z0-9+.-]*:/i.test(text)) return null;
  if (text.includes("..")) return null;
  return normalizeWorkspacePath(text);
}

export function workspacePathFromHref(href?: string): string | null {
  if (!href) return null;
  if (/^[a-z][a-z0-9+.-]*:/i.test(href)) return null;
  if (
    href.startsWith("/") ||
    href.startsWith("#") ||
    href.startsWith("?") ||
    href.startsWith("//")
  ) {
    return null;
  }
  return normalizeWorkspacePath(href);
}
