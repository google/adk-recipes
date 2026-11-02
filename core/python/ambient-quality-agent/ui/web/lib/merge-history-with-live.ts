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

import type { ChatMessage } from "@/components/chat/chat-shell";

// Cheap equality on the shape useTaskHistory's cache produces: when a turn
// completes only one task's content changed, every other entry comes back
// with the same (id, segments-ref, pending) triple as it had a moment ago.
// Returning `prev` lets React's setState bail out and skip the downstream
// MessageList reconciliation pass.
function sameMessages(a: ChatMessage[], b: ChatMessage[]): boolean {
  if (a === b) return true;
  if (a.length !== b.length) return false;
  for (let i = 0; i < a.length; i++) {
    const ma = a[i];
    const mb = b[i];
    if (ma.id !== mb.id) return false;
    if (ma.segments !== mb.segments) return false;
    if (ma.pending !== mb.pending) return false;
  }
  return true;
}

// Non-destructive id-keyed merge of replayed history into the live message
// stream. History is canonical for completed turns; optimistic/live bubbles
// (the in-flight assistant bubble from a send, and the `live:<taskId>` bubble
// from a resubscribe) are preserved until their task's canonical copy lands in
// history, then dropped to avoid a duplicate. The result is sorted by
// `createdAt` — history uses small synthetic timestamps and live uses
// Date.now()-scale, so a live turn always sorts last and the rendered order can
// never be scrambled by the backend task-list order or by an optimistic↔server
// id mismatch.
//
export function mergeHistoryWithLive(
  prev: ChatMessage[],
  history: ChatMessage[],
): ChatMessage[] {
  const historyIds = new Set(history.map((m) => m.id));
  const historyTaskIds = new Set(
    history.map((m) => m.taskId).filter((t): t is string => !!t),
  );

  const merged: ChatMessage[] = [...history];
  for (const p of prev) {
    if (historyIds.has(p.id)) continue; // superseded by the server's copy
    // An optimistic/live assistant bubble whose task now has a canonical copy
    // in history — drop it so we don't render the turn twice.
    if (p.role === "assistant" && p.taskId && historyTaskIds.has(p.taskId)) {
      continue;
    }
    merged.push(p);
  }

  merged.sort((a, b) => a.createdAt - b.createdAt);

  return sameMessages(prev, merged) ? prev : merged;
}
