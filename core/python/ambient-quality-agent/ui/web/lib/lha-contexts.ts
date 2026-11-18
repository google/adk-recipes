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

import { queryOptions } from "@tanstack/react-query";

/** One conversation the engine kept, as `/lha/contexts` reports it. */
export type StoredContext = {
  context_id: string;
  task_ids: string[];
  updated_at: string;
  /** The name saved on rename, or null when the chat was never renamed. */
  title?: string | null;
  /** The text of the chat's first user message, or null when it has none. */
  first_message?: string | null;
  /** The agent the chat is about, or null when its messages name none. */
  agent_key?: string | null;
  /** The insight the chat was started to diagnose, or null when unassociated. */
  insight_id?: string | null;
};

// Marks degraded fallback arrays (HTTP 501 or non-contexts test stubs) so
// callers can distinguish "no jobs bucket" from an authoritative empty bucket.
const fallbackContexts = new WeakSet<readonly StoredContext[]>();

function createFallbackContexts(): StoredContext[] {
  const empty: StoredContext[] = [];
  fallbackContexts.add(empty);
  return empty;
}

/**
 * Checks whether `contexts` came from an authoritative `/lha/contexts` listing
 * rather than a 501 no-bucket fallback.
 *
 * @param contexts - Cached `/lha/contexts` result, if loaded.
 * @returns True when `contexts` reflects the server's active task store.
 */
export function isAuthoritativeContextList(
  contexts: readonly StoredContext[] | undefined,
): contexts is readonly StoredContext[] {
  return Array.isArray(contexts) && !fallbackContexts.has(contexts);
}

/**
 * Fetches stored conversation summaries from `/lha/contexts`.
 *
 * @returns Stored contexts, or a marked empty fallback array on HTTP 501.
 */
export async function fetchContexts(): Promise<StoredContext[]> {
  const r = await fetch("/lha/contexts", { cache: "no-store" });
  // 501 means this deployment has no jobs bucket. That is not an empty list,
  // but for the sidebar it degrades to the same thing: show what the browser
  // knows. Throwing would blank a list the local store can still fill.
  if (r.status === 501) return createFallbackContexts();
  if (!r.ok) throw new Error(`lha-contexts ${r.status}`);
  const body = (await r.json()) as { contexts?: unknown };
  if (!Array.isArray(body?.contexts)) return createFallbackContexts();
  return body.contexts as StoredContext[];
}

/**
 * The conversations the engine keeps, as `/lha/contexts` lists them. The
 * sidebar lists them and useLhaTasks replays their tasks, so both read the
 * list through this one definition of its key and query function.
 */
export function buildLhaContextsQuery() {
  return queryOptions({
    queryKey: ["lha", "contexts"],
    queryFn: fetchContexts,
    staleTime: 10_000,
  });
}
