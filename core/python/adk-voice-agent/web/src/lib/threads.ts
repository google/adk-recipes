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

import { formatElapsed, relativeTime, sentenceCase } from "./format";
import type {
  ThreadEvent,
  ThreadOutcome,
  ThreadState,
  ThreadStatus,
  ThreadSummary,
  ThreadTurn,
} from "./types";

export function threadTitle(
  thread: Pick<ThreadSummary, "goal"> & { title?: string },
): string {
  return thread.title?.trim() || sentenceCase(thread.goal.trim());
}

export type Tone = "attention" | "running" | "success" | "danger" | "muted";

export function statusName(
  status: ThreadStatus,
  outcome: ThreadOutcome = "",
): string {
  if (status === "awaiting_approval") return "Needs you";
  if (status === "running") return "Running";
  if (outcome === "failed") return "Couldn't finish";
  if (outcome === "cancelled") return "Stopped";
  return status === "idle" ? "Done" : status;
}

export function stateTone(state: ThreadState): Tone {
  if (state === "awaiting_permission") return "attention";
  if (state === "completed") return "success";
  if (state === "failed") return "danger";
  if (state === "cancelled") return "muted";
  return "running";
}

export function threadTone(
  thread: Pick<ThreadSummary, "status" | "outcome" | "state">,
): Tone {
  if (thread.status === "awaiting_approval") return "attention";
  if (thread.status === "running") return "running";
  if (thread.outcome === "failed") return "danger";
  if (thread.outcome === "cancelled") return "muted";
  return stateTone(thread.state);
}

/** Plain-language step events for a turn's collapsible timeline. */
export function turnSteps(events: ThreadEvent[]): ThreadEvent[] {
  return events.filter(
    (e) => e.message && e.kind !== "state_changed" && e.kind !== "completed",
  );
}

/** Seconds the thread has run: live while in flight, frozen once it ended. */
export function threadElapsed(
  thread: Pick<
    ThreadSummary,
    "is_finished" | "status" | "ended_at" | "created_at" | "elapsed_seconds"
  >,
  now: number,
): number {
  if (thread.status === "idle" || thread.is_finished) {
    return thread.ended_at
      ? Math.max(0, thread.ended_at - thread.created_at)
      : thread.elapsed_seconds;
  }
  return Math.max(thread.elapsed_seconds, now - thread.created_at);
}

/** When a thread last moved: elapsed time while running, else its age. */
export function threadWhen(
  thread: Pick<
    ThreadSummary,
    | "is_finished"
    | "status"
    | "ended_at"
    | "updated_at"
    | "created_at"
    | "elapsed_seconds"
  >,
  now: number,
): string {
  return thread.status === "running"
    ? formatElapsed(threadElapsed(thread, now))
    : relativeTime(thread.ended_at ?? thread.updated_at, now);
}

/** Seconds a single turn ran: live while running, frozen once ended. */
export function turnElapsed(
  turn: Pick<
    ThreadTurn,
    "outcome" | "started_at" | "ended_at" | "elapsed_seconds"
  >,
  now: number,
): number {
  if (turn.outcome !== "running") {
    if (turn.elapsed_seconds > 0) return turn.elapsed_seconds;
    return turn.ended_at ? Math.max(0, turn.ended_at - turn.started_at) : 0;
  }
  return Math.max(turn.elapsed_seconds, now - turn.started_at);
}

/**
 * Whether a turn shows the harness's own answer. What a turn made (a Doc, a PR, an
 * event) is its answer, one tap away, and the spoken summary is its cover note; the
 * answer shows when there's nothing to open or no summary to stand in for it.
 */
export function showsAnswer(
  turn: Pick<ThreadTurn, "links" | "summary">,
): boolean {
  return (turn.links ?? []).length === 0 || !turn.summary;
}
