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

// Shared part-accumulation logic for live streaming (chat-shell.tsx),
// task history replay (task-to-segments.ts), and resubscribe (use-task-
// resubscribe.ts). Single source of truth so the three paths can't diverge.

import type { ChatMessage, MessageSegment } from "@/components/chat/chat-shell";
import type { Extracted } from "./horizon-events";

// Mark every unresolved clarify/confirmation segment that matches `callId` as
// answered, deriving the displayed text from the same payload the card sent.
// Returns the same array reference when nothing changes so callers can skip a
// re-render. Idempotent: already-answered segments (and null callIds) are
// skipped, so a double-fire is a no-op.
export function resolveHitlSegments(
  messages: ChatMessage[],
  req: { callId: string | null; confirmed: boolean; payload: Record<string, unknown> | null },
): ChatMessage[] {
  if (!req.callId) return messages;
  const answerText =
    (typeof req.payload?.choice === "string" && req.payload.choice) ||
    (typeof req.payload?.answer === "string" && req.payload.answer) ||
    null;
  let changed = false;
  const next = messages.map((m) => {
    let touched = false;
    const segments = m.segments.map((s) => {
      if (
        (s.kind === "clarify" || s.kind === "confirmation") &&
        s.callId === req.callId &&
        s.answer === undefined
      ) {
        touched = true;
        return { ...s, answer: { confirmed: req.confirmed, text: answerText } };
      }
      return s;
    });
    if (!touched) return m;
    changed = true;
    return { ...m, segments };
  });
  return changed ? next : messages;
}

// Attach orphan tool results to the matching unresolved `tool` segment by
// callId, across the whole message list. ADK splits a HITL-gated tool's
// function_call (the spinning row) and its real function_response across the
// approve echo / a continuation bubble / a new task, so the bubble-local
// attachment in appendPartsToSegments can't reach the original row. This
// bridges that gap (same idea as resolveHitlSegments for cards). Matches the
// appendPartsToSegments rule: skip already-resolved rows, require callId
// equality, never cross-attach a null callId. Idempotent; returns the same
// reference when nothing changes.
export function resolveToolResults(
  messages: ChatMessage[],
  results: { callId: string | null; name: string; result: unknown }[],
): ChatMessage[] {
  if (results.length === 0) return messages;
  const byId = new Map<string, { result: unknown }>();
  for (const r of results) {
    if (r.callId === null) continue;
    if (!byId.has(r.callId)) byId.set(r.callId, { result: r.result });
  }
  if (byId.size === 0) return messages;
  let changed = false;
  const next = messages.map((m) => {
    let touched = false;
    const segments = m.segments.map((s) => {
      if (s.kind !== "tool" || s.hasResult || s.callId === null) return s;
      const hit = byId.get(s.callId);
      if (!hit) return s;
      touched = true;
      return { ...s, result: hit.result, hasResult: true };
    });
    if (!touched) return m;
    changed = true;
    return { ...m, segments };
  });
  return changed ? next : messages;
}

export function appendPartsToSegments(
  existing: MessageSegment[],
  parts: Extracted[],
): MessageSegment[] {
  const next: MessageSegment[] = [...existing];
  parts.forEach((p) => {
    if (p.kind === "text") {
      const last = next[next.length - 1];
      if (last && last.kind === "text") {
        next[next.length - 1] = { kind: "text", text: last.text + p.text };
      } else {
        next.push({ kind: "text", text: p.text });
      }
    } else if (p.kind === "tool") {
      // A streamed function_call is surfaced more than once — the model emits
      // it in a partial AND the final aggregate event, and the converter lifts
      // both onto status messages with the same callId. Dedup by callId so the
      // row renders once; mirrors the clarify/confirmation backstop below. Null
      // callIds aren't deduped (can't tell distinct calls apart).
      const callId = p.callId;
      const duplicate =
        callId !== null &&
        next.some((s) => s.kind === "tool" && s.callId === callId);
      if (duplicate) return;
      next.push({
        kind: "tool",
        callId,
        name: p.name,
        args: p.args,
      });
    } else if (p.kind === "tool_result") {
      // Attach to the most recent matching tool segment by callId; fall back to
      // most-recent-without-result if the id is missing (some replay paths
      // strip it). Drop silently if no pending tool to attach to.
      for (let j = next.length - 1; j >= 0; j--) {
        const seg = next[j];
        if (seg.kind !== "tool" || seg.hasResult) continue;
        if (p.callId && seg.callId && seg.callId !== p.callId) continue;
        next[j] = { ...seg, result: p.result, hasResult: true };
        break;
      }
    } else if (p.kind === "clarify") {
      // The HITL gate's twin events are collapsed upstream in mapPart (the raw
      // clarify function_call is suppressed). This callId dedup is a defensive
      // backstop against the same confirmation event being replayed twice.
      const callId = p.callId;
      const duplicate =
        callId !== null &&
        next.some((s) => s.kind === "clarify" && s.callId === callId);
      if (duplicate) return;
      next.push({
        kind: "clarify",
        callId,
        question: p.question,
        choices: p.choices,
      });
    } else if (p.kind === "confirmation") {
      const callId = p.callId;
      const duplicate =
        callId !== null &&
        next.some((s) => s.kind === "confirmation" && s.callId === callId);
      if (duplicate) return;
      next.push({
        kind: "confirmation",
        callId,
        hint: p.hint,
        originalCall: p.originalCall,
        payload: p.payload,
      });
    } else if (p.kind === "hitl_result") {
      // The user's answer echo resolves the matching clarify/confirmation card
      // by callId. Within a bubble this attaches directly; the cross-bubble
      // case (echo in a separate history message) is handled in
      // task-to-segments via resolveHitlSegments.
      if (p.callId === null) return;
      for (let j = next.length - 1; j >= 0; j--) {
        const seg = next[j];
        if (
          (seg.kind === "clarify" || seg.kind === "confirmation") &&
          seg.callId === p.callId &&
          seg.answer === undefined
        ) {
          next[j] = {
            ...seg,
            answer: { confirmed: p.confirmed, text: p.text },
          };
          break;
        }
      }
    } else if (p.kind === "file") {
      next.push({ kind: "file", file: p.file });
    } else {
      next.push({ kind: "data", mime: p.mime, data: p.data });
    }
  });
  return next;
}
