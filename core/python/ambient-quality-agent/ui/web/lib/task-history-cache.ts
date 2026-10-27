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

import type { Task } from "@a2a-js/sdk";
import type { HorizonTaskSummary } from "@/lib/horizon-tasks";

export interface TaskCacheEntry {
  status: HorizonTaskSummary["status"];
  task: Task;
}

// Keep the rebuilt task data for the most-recently-viewed chats so switching
// back to one is instant. In-memory only: a tab reload clears it, which is
// intentional — reload then re-fetches and re-subscribes to any running task.
const MAX_CACHED_CHATS = 5;

// Insertion-ordered Map used as an LRU: re-inserting a key on access moves it
// to the end, so `keys().next()` is always the least-recently-used entry.
const cache = new Map<string, Map<string, TaskCacheEntry>>();

export function getTaskHistoryCache(
  contextId: string,
): Map<string, TaskCacheEntry> {
  const existing = cache.get(contextId);
  if (existing) {
    cache.delete(contextId);
    cache.set(contextId, existing);
    return existing;
  }
  const fresh = new Map<string, TaskCacheEntry>();
  cache.set(contextId, fresh);
  while (cache.size > MAX_CACHED_CHATS) {
    const lru = cache.keys().next().value as string | undefined;
    if (lru === undefined) break;
    cache.delete(lru);
  }
  return fresh;
}

export function _resetTaskHistoryCache(): void {
  cache.clear();
}
