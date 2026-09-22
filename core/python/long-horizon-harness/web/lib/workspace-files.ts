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
 * The set of workspace files, used to decide whether a filename the model
 * mentioned is real. Chat linkifies backticked filenames, and a link that
 * opens an empty panel is worse than plain text — so membership here is the
 * gate.
 *
 * Deliberately not a React Query hook: `Markdown` renders in contexts with no
 * QueryClientProvider (message-bubble tests, standalone previews), and
 * `useQueryClient` throws there. A module-level store keeps the consumer a
 * plain subscription that degrades to "no data, no links".
 */

import { useEffect, useSyncExternalStore } from "react";

const ROOT = "/workspace";

// One level below the root. Agent-written deliverables land at the root or in
// a project folder; going deeper would cost a request per directory for
// files nobody backticks.
const MAX_DEPTH = 2;

// Coalesce the burst of mounts when a long transcript renders.
const MIN_REFETCH_INTERVAL_MS = 5_000;

interface TreeEntry {
  name: string;
  kind: "file" | "dir" | "symlink";
  mtime?: number;
}

/** Workspace path -> mtime, so "the newest file this turn made" is decidable. */
export type WorkspaceFiles = ReadonlyMap<string, number>;

let cache: WorkspaceFiles | undefined;
let lastFetchedAt = 0;
let inFlight: Promise<void> | null = null;
const listeners = new Set<() => void>();

function emit(): void {
  for (const l of listeners) l();
}

async function fetchDir(path: string): Promise<TreeEntry[]> {
  const r = await fetch(
    `/lha/workspace/tree?path=${encodeURIComponent(path)}`,
    {
      cache: "no-store",
    },
  );
  if (!r.ok) throw new Error(`workspace-tree ${r.status}`);
  const body = (await r.json()) as { entries?: TreeEntry[] };
  return body.entries ?? [];
}

/**
 * The workspace path for a bare filename, or `null` when it isn't in the
 * workspace or the name is ambiguous.
 *
 * A chat attachment carries only the file's name and bytes — the artifact
 * service has no notion of where it came from. Recovering the path is what
 * lets an attachment behave like the file it is, rather than a read-only
 * snapshot of it. Ambiguity yields `null`: opening the wrong `report.md` and
 * letting the user edit it is worse than leaving the attachment read-only.
 */
export function workspacePathForName(
  files: WorkspaceFiles | undefined,
  name: string,
): string | null {
  if (!files || !name) return null;
  let found: string | null = null;
  for (const path of files.keys()) {
    if (path.slice(path.lastIndexOf("/") + 1) !== name) continue;
    if (found) return null; // same basename in two directories
    found = path;
  }
  return found;
}

// Files the agent keeps for itself. `plan.md` is prompted for by name, and
// skills and scripts are how the harness is extended — none of them are the
// answer to what the user asked, so the panel never surfaces them on its own.
const WORKING_DIRS = ["scripts/", ".agents/"];
const WORKING_NAMES = new Set(["plan.md", "todo.md"]);

/** True when `path` is agent working material rather than a user-facing file. */
export function isAgentWorkingFile(path: string): boolean {
  const rel = path.startsWith(ROOT + "/") ? path.slice(ROOT.length + 1) : path;
  if (!rel) return false;
  if (rel.split("/").some((seg) => seg.startsWith("."))) return true;
  if (WORKING_DIRS.some((d) => rel.startsWith(d))) return true;
  return WORKING_NAMES.has(rel.toLowerCase());
}

/** Fetch a fresh listing, bypassing the cache. */
export async function listWorkspaceFiles(): Promise<Map<string, number>> {
  const files = new Map<string, number>();

  const walk = async (dir: string, depth: number): Promise<void> => {
    const entries = await fetchDir(dir);
    const subDirs: string[] = [];
    for (const e of entries) {
      const full = `${dir}/${e.name}`;
      if (e.kind === "dir") {
        if (depth < MAX_DEPTH) subDirs.push(full);
      } else {
        files.set(full, e.mtime ?? 0);
      }
    }
    // Siblings in parallel; the tree endpoint is a cheap directory listing.
    await Promise.all(subDirs.map((d) => walk(d, depth + 1)));
  };

  await walk(ROOT, 1);
  return files;
}

/**
 * Refresh the listing. ``force`` bypasses the interval — used when a turn
 * ends, since the agent has usually just written the file it's about to
 * mention.
 */
export function refreshWorkspaceFiles(force = false): void {
  if (typeof fetch !== "function") return;
  if (inFlight) return;
  if (!force && Date.now() - lastFetchedAt < MIN_REFETCH_INTERVAL_MS) return;
  inFlight = listWorkspaceFiles()
    .then((files) => {
      cache = files;
      emit();
    })
    .catch(() => {
      // A failed listing just means nothing gets linkified — strictly better
      // than rendering a link we can't honour.
    })
    .finally(() => {
      lastFetchedAt = Date.now();
      inFlight = null;
    });
}

function subscribe(cb: () => void): () => void {
  listeners.add(cb);
  return () => listeners.delete(cb);
}

const getSnapshot = (): WorkspaceFiles | undefined => cache;

/** Test seam. */
export function __resetWorkspaceFiles(): void {
  cache = undefined;
  lastFetchedAt = 0;
  inFlight = null;
  listeners.clear();
}

/**
 * Known workspace files, refreshed on mount and whenever a turn ends.
 * Returns ``undefined`` until the first listing lands.
 */
export function useWorkspaceFiles(busy: boolean): WorkspaceFiles | undefined {
  useEffect(() => {
    // Turn end is the one moment we know the tree may have changed.
    refreshWorkspaceFiles(!busy);
  }, [busy]);
  return useSyncExternalStore(subscribe, getSnapshot, getSnapshot);
}
