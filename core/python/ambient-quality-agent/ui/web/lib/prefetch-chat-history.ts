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

import type { HorizonClient } from "@/lib/a2a-client";
import { buildLhaContextsQuery } from "@/lib/horizon-sessions";
import {
  buildLhaTasksQuery,
  buildTaskList,
  isTerminalTaskStatus,
} from "@/lib/horizon-tasks";
import { queryClient } from "@/lib/query-client";
import { getTaskHistoryCache } from "@/lib/task-history-cache";

// Warm both layers for a not-yet-open chat so clicking it renders instantly:
// the TanStack task list (useLhaTasks's own query) and the per-task history
// cache, for the tasks the server stores as well. getTask is context-agnostic, so the caller's active client can fetch
// any chat's tasks. Best-effort: failures are swallowed.
const inFlight = new Set<string>();

export async function prefetchChatHistory(
  contextId: string,
  client: HorizonClient | null,
): Promise<void> {
  if (!client || inFlight.has(contextId)) return;
  inFlight.add(contextId);
  try {
    const recorded = await queryClient.fetchQuery(
      buildLhaTasksQuery(contextId),
    );
    const tasks = buildTaskList(
      contextId,
      recorded,
      queryClient.getQueryData(buildLhaContextsQuery().queryKey),
    );
    const cache = getTaskHistoryCache(contextId);
    // Mirror useTaskHistory: only terminal tasks are cacheable, and skip ones
    // already cached with a matching status. A second hover after warming thus
    // does no getTask work.
    const toFetch = tasks.filter(
      (t) =>
        isTerminalTaskStatus(t.status) && cache.get(t.id)?.status !== t.status,
    );
    await Promise.all(
      toFetch.map(async (t) => {
        try {
          const task = await client.getTask(t.id);
          cache.set(t.id, { status: t.status, task });
        } catch {
          // A single task failing must not abort the rest of the prefetch.
        }
      }),
    );
  } catch {
    // Task-list fetch failed — nothing warmed; the real open will fetch.
  } finally {
    inFlight.delete(contextId);
  }
}
