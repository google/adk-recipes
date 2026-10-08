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

// Convert an A2A Task (history + artifacts) into the ChatMessage[] shape the
// chat view renders. Replay-only — the live-stream path stays on
// chat-shell.tsx + appendPartsToSegments. Both call the same per-part
// extractor (mapPart) so the rendering stays consistent.

import { TaskState } from "@a2a-js/sdk";
import type { Part, Task } from "@a2a-js/sdk";
import { isUserRole } from "./a2a-part";
import type { ChatMessage, MessageSegment } from "@/components/chat/chat-shell";
import {
  appendPartsToSegments,
  resolveHitlSegments,
  resolveToolResults,
} from "@/lib/chat-segments";
import { mapPart, type Extracted } from "@/lib/horizon-events";

type HitlResult = Extract<Extracted, { kind: "hitl_result" }>;

// Every adk_request_confirmation function_response (the user's approve/decline
// echo) in a task's history. The echo arrives as its own role:"user" message,
// separate from the bubble holding the card, so it's collected independently of
// segment assembly.
function extractHitlResults(task: Task): HitlResult[] {
  const out: HitlResult[] = [];
  for (const message of task.history ?? []) {
    for (const part of message.parts ?? []) {
      const mapped = mapPart(part);
      if (mapped?.kind === "hitl_result") out.push(mapped);
    }
  }
  return out;
}

// Resolve each clarify/confirmation card to its answered state from the matching
// echo (by callId). resolveHitlSegments derives the displayed text from
// payload.choice/answer; the echo already carries it, so it's passed through the
// answer key it understands. Idempotent: already-answered cards are skipped and
// the same reference is returned when nothing changes.
function applyHitlResults(
  messages: ChatMessage[],
  results: HitlResult[],
): ChatMessage[] {
  let resolved = messages;
  for (const r of results) {
    resolved = resolveHitlSegments(resolved, {
      callId: r.callId,
      confirmed: r.confirmed,
      payload: r.text === null ? null : { answer: r.text },
    });
  }
  return resolved;
}

// ADK resumes an approved tool call into a NEW A2A task, so a confirmation card
// lands in one task while its echo lands in the next. Each task renders
// independently, so a per-task resolve can't see the echo — leaving the card
// stuck on its live controls after reload. This bridges the split: collect every
// echo across all tasks and resolve the assembled message list by callId.
export function resolveHitlAcrossTasks(
  messages: ChatMessage[],
  tasks: Task[],
): ChatMessage[] {
  let resolved = messages;
  for (const task of tasks) {
    resolved = applyHitlResults(resolved, extractHitlResults(task));
  }
  return resolved;
}

type ToolResult = Extract<Extracted, { kind: "tool_result" }>;

// Every tool function_response in a task's history. The HITL-gated tool's real
// response arrives after the approve echo (a role:"user" message that flushes
// the bubble) or in a later task, so it's collected independently of the
// bubble-by-bubble assembly — same pattern as extractHitlResults.
function extractToolResults(task: Task): ToolResult[] {
  const out: ToolResult[] = [];
  for (const message of task.history ?? []) {
    for (const part of message.parts ?? []) {
      const mapped = mapPart(part);
      if (mapped?.kind === "tool_result") out.push(mapped);
    }
  }
  return out;
}

// Attach every tool result across all of a context's tasks to its matching
// unresolved tool segment by callId. ADK resumes an approved tool call into a
// new task, so the function_response can land in a later task than its
// function_call — the per-task assembly can't bridge that. Mirrors
// resolveHitlAcrossTasks.
export function resolveToolResultsAcrossTasks(
  messages: ChatMessage[],
  tasks: Task[],
): ChatMessage[] {
  let resolved = messages;
  for (const task of tasks) {
    resolved = resolveToolResults(resolved, extractToolResults(task));
  }
  return resolved;
}

export interface TaskToSegmentsArgs {
  task: Task;
}

// Route through the codec to handle both the numeric enum and the string form
// in persisted task history.
function _role(role: unknown): "user" | "assistant" | "system" {
  if (isUserRole(role)) return "user";
  if (role === "system") return "system";
  return "assistant";
}

// A Message lacks a wall-clock timestamp, so we synthesize a stable ordering
// from the task id + index. mergeHistoryWithLive sorts by createdAt; tasks
// within a context land in insertion order from the endpoint, so spacing
// per-task by 1ms is enough to preserve order without colliding. Must stay well
// below Date.now() scale — the live↔history merge relies on optimistic bubbles
// (Date.now()) sorting after every history bubble.
function _syntheticTimestamp(taskIndex: number, msgIndex: number): number {
  return taskIndex * 1000 + msgIndex;
}

export function taskToChatMessages(
  args: TaskToSegmentsArgs & { taskIndex: number },
): ChatMessage[] {
  const { task, taskIndex } = args;
  const out: ChatMessage[] = [];
  const history = task.history ?? [];

  // ADK splits one assistant turn across several history messages — the
  // function_call, the function_response, and the final text each arrive as
  // their own `role: "agent"` message. A tool_result only attaches to its
  // tool segment when both ride the same segment array, so a run of consecutive
  // agent/system messages must accumulate into one bubble. Otherwise the
  // function_response lands in an empty array, gets dropped, and the tool row
  // spins forever on replay. This mirrors the live + resubscribe paths, which
  // accumulate a whole turn into a single bubble.
  let pending: ChatMessage | null = null;
  const flush = () => {
    if (pending && pending.segments.length > 0) out.push(pending);
    pending = null;
  };

  history.forEach((message, msgIndex) => {
    // Status-update messages tagged adk_partial=true are streaming partials
    // paired with a final aggregate; skip them so the aggregate renders once.
    const metadata = message.metadata as Record<string, unknown> | undefined;
    if (metadata?.adk_partial === true) return;
    const role = _role(message.role);
    const parts = message.parts ?? [];
    const mapped = parts.map(mapPart).filter((x): x is Extracted => x !== null);
    if (mapped.length === 0) return;

    if (role === "user") {
      // User bubbles only render text/file/data — never tool calls or
      // confirmations, which only the assistant emits. Filter to keep the
      // same render contract the live stream's user echo uses.
      flush();
      const segments: MessageSegment[] = [];
      mapped.forEach((p) => {
        if (p.kind === "text") {
          segments.push({ kind: "text", text: p.text });
        } else if (p.kind === "file") {
          segments.push({ kind: "file", file: p.file });
        } else if (p.kind === "data") {
          segments.push({ kind: "data", mime: p.mime, data: p.data });
        }
      });
      if (segments.length === 0) return;
      // The user bubble's id must equal the messageId the client sent (a2a
      // stores the initiating message verbatim), so mergeHistoryWithLive can
      // dedupe the optimistic user bubble against this canonical copy by id.
      out.push({
        id: message.messageId,
        role,
        createdAt: _syntheticTimestamp(taskIndex, msgIndex),
        segments,
        taskId: task.id,
      });
      return;
    }

    if (pending && pending.role !== role) flush();
    if (!pending) {
      pending = {
        id: message.messageId,
        role,
        createdAt: _syntheticTimestamp(taskIndex, msgIndex),
        segments: [],
        taskId: task.id,
      };
    }
    pending.segments = appendPartsToSegments(pending.segments, mapped);
  });
  flush();

  // Fold extra parts (artifacts, pending input request) into this task's
  // assistant turn so the reply renders in one bubble; tool chips already came
  // from history above. Falls back to a new bubble if the turn had none.
  const foldIntoTurn = (
    rawParts: Part[],
    fallbackId: string,
    fallbackCreatedAt: number,
  ) => {
    const mapped = rawParts
      .map(mapPart)
      .filter((x): x is Extracted => x !== null);
    if (mapped.length === 0) return;
    let bubble: ChatMessage | undefined;
    for (let i = out.length - 1; i >= 0; i--) {
      if (out[i].role === "assistant" && out[i].taskId === task.id) {
        bubble = out[i];
        break;
      }
    }
    if (bubble) {
      bubble.segments = appendPartsToSegments(bubble.segments, mapped);
    } else {
      out.push({
        id: fallbackId,
        role: "assistant",
        createdAt: fallbackCreatedAt,
        segments: appendPartsToSegments([], mapped),
        taskId: task.id,
      });
    }
  };

  // Text and files arrive as artifacts; tool calls come from history. A
  // persisted Task has no shared sort key across these two channels, so all
  // artifact text renders after the turn's tool chips on reload. Live rendering
  // keeps true arrival order; only reload reorders.
  const artifactParts: Part[] = (task.artifacts ?? []).flatMap(
    (a) => a.parts ?? [],
  );
  foldIntoTurn(
    artifactParts,
    `${task.id}:artifacts`,
    _syntheticTimestamp(taskIndex, history.length),
  );

  // A task paused for input (HITL) carries the pending request — the
  // adk_request_confirmation DataPart — in status.message, not history. Fold it
  // in so the confirmation/clarify card renders on replay and survives the
  // post-turn history refetch (without this the live card vanishes when history
  // replaces the bubble).
  // A failed or rejected task carries its error there too, and nowhere else.
  const statusState = task.status?.state;
  if (
    statusState === TaskState.TASK_STATE_INPUT_REQUIRED ||
    statusState === TaskState.TASK_STATE_AUTH_REQUIRED ||
    statusState === TaskState.TASK_STATE_FAILED ||
    statusState === TaskState.TASK_STATE_REJECTED
  ) {
    const statusParts = task.status?.message?.parts ?? [];
    foldIntoTurn(
      statusParts,
      `${task.id}:input`,
      _syntheticTimestamp(taskIndex, history.length + 1),
    );
  }

  // A card and its echo can share this task or straddle two; resolveHitlAcrossTasks
  // re-runs this over the full multi-task list to bridge the cross-task case.
  const withHitl = applyHitlResults(out, extractHitlResults(task));
  // A HITL-gated tool's function_response arrives after the approve echo flushed
  // its bubble, so the bubble-local attachment dropped it — re-attach here.
  return resolveToolResults(withHitl, extractToolResults(task));
}
