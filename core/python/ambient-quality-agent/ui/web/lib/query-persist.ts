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
 * Persist AQuA's query cache to localStorage so a reload paints from disk.
 *
 * Every `["aqua", ...]` read goes through the agent and takes seconds, so
 * without this a refresh means staring at spinners for data that has not
 * changed. Hydrated entries keep their original `dataUpdatedAt`, so anything
 * past its `staleTime` refetches in the background: the paint is instant, the
 * data still converges. Nothing here is authoritative.
 *
 * Hand-rolled rather than `@tanstack/query-persist-client-core` because it is
 * one dependency for forty lines, and only the `aqua` keys are wanted.
 */
import type { QueryClient } from "@tanstack/react-query";

const KEY = "aqua.query-cache.v1";
/** Older than this and the paint would be misleading rather than helpful. */
const MAX_AGE_MS = 24 * 60 * 60 * 1000;
const WRITE_DEBOUNCE_MS = 300;

interface PersistedQuery {
  key: readonly unknown[];
  data: unknown;
  updatedAt: number;
}

function storage(): Storage | null {
  try {
    return typeof window === "undefined" ? null : window.localStorage;
  } catch {
    // Storage can throw outright in a locked-down or private context.
    return null;
  }
}

/** Only successful AQuA reads. Chat tasks live in the conversation store. */
function shouldPersist(key: readonly unknown[]): boolean {
  return key[0] === "aqua";
}

export function hydrateQueryCache(client: QueryClient): void {
  const store = storage();
  if (!store) return;
  let entries: PersistedQuery[];
  try {
    entries = JSON.parse(store.getItem(KEY) ?? "[]") as PersistedQuery[];
  } catch {
    store.removeItem(KEY);
    return;
  }
  if (!Array.isArray(entries)) return;

  const cutoff = Date.now() - MAX_AGE_MS;
  for (const entry of entries) {
    if (!entry?.key || entry.updatedAt < cutoff) continue;
    client.setQueryData(entry.key, entry.data, { updatedAt: entry.updatedAt });
  }
}

export function persistQueryCache(client: QueryClient): () => void {
  const store = storage();
  if (!store) return () => {};

  let timer: ReturnType<typeof setTimeout> | undefined;
  const write = () => {
    const entries: PersistedQuery[] = [];
    for (const query of client.getQueryCache().getAll()) {
      if (query.state.status !== "success") continue;
      if (!shouldPersist(query.queryKey)) continue;
      entries.push({
        key: query.queryKey,
        data: query.state.data,
        updatedAt: query.state.dataUpdatedAt,
      });
    }
    try {
      store.setItem(KEY, JSON.stringify(entries));
    } catch {
      // Over quota: drop the cache rather than wedging every later write.
      store.removeItem(KEY);
    }
  };

  const unsubscribe = client.getQueryCache().subscribe(() => {
    if (timer) clearTimeout(timer);
    timer = setTimeout(write, WRITE_DEBOUNCE_MS);
  });

  // Without this the debounce loses the last write on a reload or a tab close,
  // which is exactly when the cache is about to be needed: a measured run
  // persisted only the three fastest queries and none of the four big ones.
  const flush = () => {
    if (timer) clearTimeout(timer);
    write();
  };
  const onHide = () => {
    if (document.visibilityState === "hidden") flush();
  };
  window.addEventListener("pagehide", flush);
  document.addEventListener("visibilitychange", onHide);

  return () => {
    if (timer) clearTimeout(timer);
    window.removeEventListener("pagehide", flush);
    document.removeEventListener("visibilitychange", onHide);
    unsubscribe();
  };
}
