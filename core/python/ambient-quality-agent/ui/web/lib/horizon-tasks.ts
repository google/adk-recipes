"use client";
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

import { useCallback, useMemo } from "react";

import { taskIdsFor } from "@/lib/aqua-conversations";
import { queryOptions, useQuery, useQueryClient } from "@tanstack/react-query";
import { buildLhaContextsQuery, type StoredContext } from "./horizon-sessions";
import { qk } from "./query-keys";

export interface HorizonTaskSummary {
  id: string;
  status:
    | "submitted"
    | "working"
    | "input-required"
    | "completed"
    | "canceled"
    | "failed"
    | "rejected"
    | "auth-required"
    | "unknown";
  isActive: boolean;
}

const TERMINAL_STATES: ReadonlySet<HorizonTaskSummary["status"]> = new Set([
  "completed",
  "canceled",
  "failed",
  "rejected",
]);

export function isTerminalTaskStatus(
  status: HorizonTaskSummary["status"],
): boolean {
  return TERMINAL_STATES.has(status);
}

/**
 * The tasks this browser recorded for a conversation, oldest first; useLhaTasks
 * adds the ones the server stores. Anything that warms this cache has to go
 * through this too: a second query function under the same key caches its own
 * shape, and useLhaTasks then reads that.
 */
export function buildLhaTasksQuery(contextId: string) {
  return queryOptions({
    queryKey: qk.tasks(contextId),
    // Adapted from horizon, which fetches `/lha/contexts/{id}/tasks`. AQuA
    // answers that only when it has a jobs bucket, and with the stored tasks
    // rather than this list, so the ids are recorded in the browser alongside
    // the conversation as well. Every task here has already finished -- a
    // live turn streams, it is not replayed.
    queryFn: async (): Promise<HorizonTaskSummary[]> =>
      taskIdsFor(contextId).map((id) => ({
        id,
        status: "completed" as const,
        isActive: false,
      })),
    staleTime: 2_000,
  });
}

/**
 * Builds a conversation's task list, oldest first, from the tasks this browser
 * recorded and the ids `/lha/contexts` reports for it. The recorded list
 * stands when it holds every stored task. Otherwise a turn went unrecorded
 * here (its stream ended before naming its task, or it ran in another
 * browser): the stored ids come first, in the server's order, then any
 * recorded task the server has not stored yet.
 */
export function buildTaskList(
  contextId: string,
  recorded: HorizonTaskSummary[],
  contexts: StoredContext[] | undefined,
): HorizonTaskSummary[] {
  const stored =
    contexts?.find((c) => c.context_id === contextId)?.task_ids ?? [];
  const recordedIds = new Set(recorded.map((t) => t.id));
  if (stored.every((id) => recordedIds.has(id))) return recorded;
  const storedIds = new Set(stored);
  return [
    ...stored.map((id) => ({
      id,
      status: "completed" as const,
      isActive: false,
    })),
    ...recorded.filter((t) => !storedIds.has(t.id)),
  ];
}

export interface UseLhaTasksResult {
  tasks: HorizonTaskSummary[] | undefined;
  activeTaskId: string | null;
  error: Error | undefined;
  isLoading: boolean;
  refresh: () => Promise<unknown>;
}

export function useLhaTasks(contextId: string | null): UseLhaTasksResult {
  const qc = useQueryClient();
  const q = useQuery({
    ...buildLhaTasksQuery(contextId ?? ""),
    enabled: !!contextId,
    refetchInterval: 10_000,
    refetchOnWindowFocus: true,
  });
  const contexts = useQuery({
    ...buildLhaContextsQuery(),
    enabled: !!contextId,
  });
  const tasks = useMemo(() => {
    if (!contextId || !q.data) return undefined;
    // Nothing recorded and no answer from the server yet is not an empty chat.
    if (q.data.length === 0 && contexts.isPending) return undefined;
    return buildTaskList(contextId, q.data, contexts.data);
  }, [contextId, q.data, contexts.data, contexts.isPending]);
  const refresh = useCallback(
    () => qc.invalidateQueries({ queryKey: qk.tasks(contextId ?? "") }),
    [qc, contextId],
  );
  return {
    tasks,
    // A stale pointer over a settled task would lock the composer forever.
    activeTaskId:
      tasks?.find((t) => t.isActive && !isTerminalTaskStatus(t.status))?.id ??
      null,
    error: q.error ?? undefined,
    isLoading: !!contextId && tasks === undefined,
    refresh,
  };
}
