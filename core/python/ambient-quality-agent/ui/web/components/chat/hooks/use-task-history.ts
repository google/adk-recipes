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

import { useEffect, useRef, useState } from "react";
import { TaskState, type Task } from "@a2a-js/sdk";
import type { HorizonClient } from "@/lib/a2a-client";
import {
  resolveHitlAcrossTasks,
  resolveToolResultsAcrossTasks,
  taskToChatMessages,
} from "@/lib/task-to-segments";
import {
  isTerminalTaskStatus,
  type HorizonTaskSummary,
} from "@/lib/horizon-tasks";
import {
  getTaskHistoryCache,
  type TaskCacheEntry,
} from "@/lib/task-history-cache";
import type { ChatMessage } from "../chat-shell";

export interface UseTaskHistoryArgs {
  contextId: string | null;
  tasks: HorizonTaskSummary[] | undefined;
  client: HorizonClient | null;
  // Bumped by useTaskResubscribe on each stream frame. A ref, so it can never
  // enter a dep array and restart the subscription.
  progressRef?: { current: number };
}

// Fast cadence while a stream is feeding us; the slow arm is the fallback for
// when resubscribe cannot attach and no frame ever arrives.
const TICK_MS = 2_000;
const IDLE_REFRESH_MS = 10_000;

// The stream can end before the server has stored a task's final state, so a
// getTask right after a turn may still answer submitted or working, with none
// of the reply. The task list marks every recorded task completed, so that
// copy would be cached for good and the turn's reply and card would drop out
// of the transcript. Such a copy is retried instead, a bounded number of times.
const STALE_RETRY_MS = 1_500;
const STALE_RETRIES = 8;

function _isUnsettled(task: Task): boolean {
  const state = task.status?.state;
  return (
    state === TaskState.TASK_STATE_SUBMITTED ||
    state === TaskState.TASK_STATE_WORKING
  );
}

// A concurrent TaskManager can serve an older copy of the same in-flight task.
// Both counts only grow for a live task, so a non-superset answer is stale.
function _taskSize(t: Task): { turns: number; parts: number } {
  const turns = t.history?.length ?? 0;
  let parts = 0;
  for (const a of t.artifacts ?? []) parts += a.parts?.length ?? 0;
  return { turns, parts };
}

function _isSuperset(next: Task, prev: Task): boolean {
  const a = _taskSize(next);
  const b = _taskSize(prev);
  return a.turns >= b.turns && a.parts >= b.parts;
}

export interface UseTaskHistoryResult {
  historyMessages: ChatMessage[] | null;
  historyLoading: boolean;
}

// Stable signature so we only re-run when task identities or status actually
// change. isActive flips on every turn but doesn't affect history; excluding it
// avoids refetching every completed task on each transition.
function _tasksSignature(tasks: HorizonTaskSummary[] | undefined): string {
  if (!tasks) return "";
  return tasks.map((t) => `${t.id}:${t.status}`).join("|");
}

export function useTaskHistory({
  contextId,
  tasks,
  client,
  progressRef,
}: UseTaskHistoryArgs): UseTaskHistoryResult {
  const [historyMessages, setHistoryMessages] = useState<ChatMessage[] | null>(
    null,
  );
  const [historyLoading, setHistoryLoading] = useState(false);
  const signature = _tasksSignature(tasks);
  // Last good copy of an in-flight task. Deliberately NOT the shared cache: a
  // `working` entry there would satisfy the `toFetch` check and the task would
  // never be refetched again.
  const retainedRef = useRef<Map<string, Task>>(new Map());
  const didFirstLoadRef = useRef(false);

  // biome-ignore lint/correctness/useExhaustiveDependencies(contextId): clear history state when switching chats.
  useEffect(() => {
    retainedRef.current.clear();
    didFirstLoadRef.current = false;
    // The previous chat's transcript must not stand in for this one's.
    setHistoryMessages(null);
    setHistoryLoading(false);
  }, [contextId]);

  // biome-ignore lint/correctness/useExhaustiveDependencies: signature stands in for tasks, whose identity changes on every poll, and the poll timer reads progressRef live rather than as a trigger.
  useEffect(() => {
    if (tasks === undefined) {
      // tasks undefined = query still loading. Don't clobber a previous render
      // with null while a refresh is in flight.
      return;
    }
    if (!contextId || tasks.length === 0) {
      setHistoryMessages([]);
      setHistoryLoading(false);
      return;
    }

    // Per-chat cache survives client swaps and component switches, keyed by
    // contextId. Only terminal tasks are stored (see below), so an in-flight
    // task is always re-fetched and re-subscribed.
    const entries = getTaskHistoryCache(contextId);

    // Evict cache entries no longer in `tasks` so they don't leak across edits.
    const liveIds = new Set(tasks.map((t) => t.id));
    for (const id of entries.keys()) {
      if (!liveIds.has(id)) entries.delete(id);
    }

    const rebuildFrom = (
      source: Map<string, TaskCacheEntry>,
    ): ChatMessage[] => {
      const rebuilt: ChatMessage[] = [];
      // The same messageId can appear in multiple tasks' history (ADK carries
      // the initiating user message into each follow-up task). Keep the first
      // occurrence so React keys stay unique and ordering is stable.
      const seenIds = new Set<string>();
      const orderedTasks: Task[] = [];
      for (let i = 0; i < tasks.length; i++) {
        const entry = source.get(tasks[i].id);
        if (!entry) continue;
        orderedTasks.push(entry.task);
        const msgs = taskToChatMessages({
          task: entry.task,
          taskIndex: i,
        });
        for (const msg of msgs) {
          if (seenIds.has(msg.id)) continue;
          seenIds.add(msg.id);
          rebuilt.push(msg);
        }
      }
      // A confirmation card and its approve/decline echo can straddle two
      // tasks (ADK resumes into a new task), so resolve HITL across the whole
      // assembled list — the per-task resolve inside taskToChatMessages can't.
      const withHitl = resolveHitlAcrossTasks(rebuilt, orderedTasks);
      // Likewise a HITL-gated tool's real function_response can land in a later
      // task than its spinning function_call; bridge it so the row stops
      // spinning on reload.
      return resolveToolResultsAcrossTasks(withHitl, orderedTasks);
    };

    // A task needs fetching if it isn't cached with a matching status. Because
    // we only cache terminal tasks, any non-terminal task is always here.
    const toFetch = tasks.filter((t) => entries.get(t.id)?.status !== t.status);

    // Fast path: every task is terminal and already cached. Rebuild from cache
    // synchronously — no getTask() calls, no loading flash, and no need to wait
    // for the A2A client to finish booting.
    if (toFetch.length === 0) {
      setHistoryMessages(rebuildFrom(entries));
      didFirstLoadRef.current = true;
      setHistoryLoading(false);
      return;
    }

    if (!client) {
      // Can't fetch the missing/changed tasks until the client boots. Report
      // that as loading: an empty transcript reads as a new chat.
      if (!didFirstLoadRef.current) setHistoryLoading(true);
      return;
    }

    let cancelled = false;
    let staleRetries = 0;
    let staleTimer: ReturnType<typeof setTimeout> | undefined;
    // Token rather than a boolean: a slow fetch completing after a newer one
    // started must not release the gate for the live one.
    let token = 0;
    let inFlight = 0;
    let lastCompletedAt = 0;
    let seenProgress = progressRef?.current ?? 0;

    const runFetch = async () => {
      const mine = ++token;
      inFlight = mine;
      if (!didFirstLoadRef.current) setHistoryLoading(true);
      try {
        // A retry skips what the first pass already cached.
        const pending = toFetch.filter(
          (t) => entries.get(t.id)?.status !== t.status,
        );
        const fetched = await Promise.all(
          pending.map(async (s) => {
            try {
              const task = await client.getTask(s.id);
              return { id: s.id, status: s.status, task };
            } catch (err) {
              // Don't cache failures — a transient backend hiccup must not hide
              // the task forever. The next tick retries.
              console.warn(`useTaskHistory: skipping task ${s.id}`, err);
              return null;
            }
          }),
        );
        if (cancelled || mine !== token) return;
        // A recorded task can come back older than the turn that streamed it;
        // keep it out of the cache and, while retries last, out of the
        // transcript too, where the live bubble still shows the turn.
        const staleIds = new Set(
          fetched
            .filter(
              (r): r is NonNullable<typeof r> =>
                !!r && isTerminalTaskStatus(r.status) && _isUnsettled(r.task),
            )
            .map((r) => r.id),
        );
        const retryStale = staleIds.size > 0 && staleRetries < STALE_RETRIES;
        for (const r of fetched) {
          if (!r) continue;
          if (staleIds.has(r.id)) continue;
          // Only cache terminal tasks; in-flight ones must always be re-fetched
          // and re-subscribed so a running background turn is never served stale.
          if (isTerminalTaskStatus(r.status)) {
            entries.set(r.id, { status: r.status, task: r.task });
            retainedRef.current.delete(r.id);
          } else {
            // Rebuild needs the task object even for an in-flight turn, but it
            // must not persist — drop any stale terminal entry under this id.
            entries.delete(r.id);
          }
        }

        // Overlay the just-fetched tasks (including the in-flight one we did not
        // persist) on top of the cached entries so they render this pass.
        const overlay = new Map(entries);
        for (const s of toFetch) {
          const r = fetched.find((f) => f && f.id === s.id) ?? null;
          if (retryStale && staleIds.has(s.id)) continue;
          const prev = retainedRef.current.get(s.id);
          let task = r?.task;
          if (!task) {
            // Failed refetch: keep showing the last good copy rather than
            // dropping the whole turn out of the transcript.
            if (!prev) continue;
            task = prev;
          } else if (
            prev &&
            !isTerminalTaskStatus(s.status) &&
            !_isSuperset(task, prev)
          ) {
            task = prev;
          }
          if (!isTerminalTaskStatus(s.status)) {
            retainedRef.current.set(s.id, task);
          }
          overlay.set(s.id, { status: s.status, task });
        }
        setHistoryMessages(rebuildFrom(overlay));
        didFirstLoadRef.current = true;
        setHistoryLoading(false);
        if (retryStale) {
          staleRetries += 1;
          staleTimer = setTimeout(() => void runFetch(), STALE_RETRY_MS);
        }
      } finally {
        // Always release, even on teardown, or refresh dies permanently.
        if (inFlight === mine) inFlight = 0;
        lastCompletedAt = Date.now();
      }
    };

    void runFetch();

    // Only a live task can change under us; a settled list needs no clock.
    const anyActive = tasks.some((t) => !isTerminalTaskStatus(t.status));
    let timer: ReturnType<typeof setInterval> | undefined;
    if (anyActive) {
      timer = setInterval(() => {
        if (cancelled || inFlight) return;
        const now = Date.now();
        if (now - lastCompletedAt < TICK_MS) return;
        const progressed = (progressRef?.current ?? 0) !== seenProgress;
        if (!progressed && now - lastCompletedAt < IDLE_REFRESH_MS) return;
        seenProgress = progressRef?.current ?? 0;
        void runFetch();
      }, TICK_MS);
    }

    return () => {
      cancelled = true;
      if (timer) clearInterval(timer);
      if (staleTimer) clearTimeout(staleTimer);
    };
  }, [signature, contextId, client]);

  return { historyMessages, historyLoading };
}
