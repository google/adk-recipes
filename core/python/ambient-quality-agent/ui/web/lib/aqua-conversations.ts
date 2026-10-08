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

/** The chat list, held in this browser.
 *
 * A conversation is an A2A `contextId`. The engine mints one ADK session per
 * context under the user `A2A_USER_<contextId>`, so listing them server-side
 * would need the user ids we are trying to discover in the first place --
 * `/lha/sessions` answers an empty list for exactly that reason.
 *
 * Holding them here is honest about what the list is: conversations started on
 * this machine, not every conversation ever. The transcript itself lives in the
 * engine, so opening one on another machine still works if you have the id.
 */

import { sliceGraphemes } from "./graphemes";

const KEY = "aqua.conversations";
const LIMIT = 50;

export interface StoredConversation {
  id: string;
  title: string;
  /** Epoch milliseconds. */
  lastUpdated: number;
  createdAt: number;
  /**
   * A2A task ids for this conversation, oldest first, recorded as each turn
   * streams.
   *
   * A deployment without a jobs bucket stores no task list, so this is the
   * only one it has; useLhaTasks adds the ids `/lha/contexts` reports where
   * there is one. The messages themselves still come from A2A `getTask`.
   */
  taskIds?: string[];
}

// Falls back to memory when there is no Storage: happy-dom does not provide
// one, and a chat list that throws in tests is worse than one that does not
// survive a reload there.
let memory = "[]";

const inMemory: Pick<Storage, "getItem" | "setItem"> = {
  getItem: () => memory,
  setItem: (_k, v) => {
    memory = v;
  },
};

function storage(): Pick<Storage, "getItem" | "setItem"> {
  try {
    const local = typeof window !== "undefined" ? window.localStorage : null;
    if (local) {
      // Probe it: Safari in private mode has the API and throws on write.
      local.getItem(KEY);
      return local;
    }
  } catch {
    /* fall through */
  }
  return inMemory;
}

export function listConversations(): StoredConversation[] {
  const s = storage();
  try {
    const raw = JSON.parse(s.getItem(KEY) ?? "[]") as unknown;
    if (!Array.isArray(raw)) return [];
    return raw
      .filter(
        (c): c is StoredConversation =>
          typeof (c as StoredConversation)?.id === "string",
      )
      .sort((a, b) => b.lastUpdated - a.lastUpdated);
  } catch {
    return [];
  }
}

/** Record a conversation, keeping the first message as its title. */
export function rememberConversation(id: string, firstMessage?: string): void {
  const s = storage();
  const now = Date.now();
  const existing = listConversations();
  const previous = existing.find((c) => c.id === id);
  const next: StoredConversation = {
    id,
    title: previous?.title || titleFrom(firstMessage) || "New chat",
    createdAt: previous?.createdAt ?? now,
    lastUpdated: now,
    taskIds: previous?.taskIds,
  };
  const rest = existing.filter((c) => c.id !== id);
  s.setItem(KEY, JSON.stringify([next, ...rest].slice(0, LIMIT)));
  notify();
}

export function forgetConversation(id: string): void {
  const s = storage();
  s.setItem(
    KEY,
    JSON.stringify(listConversations().filter((c) => c.id !== id)),
  );
  notify();
}

export function renameConversation(id: string, title: string): void {
  const s = storage();
  const next = listConversations().map((c) =>
    c.id === id ? { ...c, title } : c,
  );
  s.setItem(KEY, JSON.stringify(next));
  notify();
}

function titleFrom(message?: string): string {
  const trimmed = (message ?? "").trim().replace(/\s+/g, " ");
  if (!trimmed) return "";
  return trimmed.length > 60 ? `${sliceGraphemes(trimmed, 57)}…` : trimmed;
}

// The list changes from the chat pane but is rendered by the sidebar, so
// changes have to reach a component that never called the mutation.
const listeners = new Set<() => void>();

function notify(): void {
  for (const l of listeners) l();
}

export function subscribeConversations(listener: () => void): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

/** Record a completed turn's task id so the conversation can be replayed. */
export function rememberTask(conversationId: string, taskId: string): void {
  const existing = listConversations();
  const previous = existing.find((c) => c.id === conversationId);
  // A conversation the user never sent to has no entry yet; create one so the
  // task is not dropped on the floor.
  const base: StoredConversation = previous ?? {
    id: conversationId,
    title: "New chat",
    createdAt: Date.now(),
    lastUpdated: Date.now(),
  };
  const ids = base.taskIds ?? [];
  if (ids.includes(taskId)) return;
  const next = { ...base, taskIds: [...ids, taskId], lastUpdated: Date.now() };
  const rest = existing.filter((c) => c.id !== conversationId);
  storage().setItem(KEY, JSON.stringify([next, ...rest].slice(0, LIMIT)));
  notify();
}

/** Task ids for a conversation, oldest first. */
export function taskIdsFor(conversationId: string): string[] {
  return (
    listConversations().find((c) => c.id === conversationId)?.taskIds ?? []
  );
}

/** Drop every stored conversation. Test-only: isolates one spec from the next. */
export function resetConversationsForTest(): void {
  storage().setItem(KEY, "[]");
  notify();
}

// useSyncExternalStore compares snapshots by identity, and listConversations
// parses fresh objects on every call, so returning it directly is an infinite
// render loop (React error #185). Memoise on the raw string instead.
let snapshotRaw: string | null = null;
let snapshot: StoredConversation[] = [];

/** The conversation list as a stable reference, for useSyncExternalStore. */
export function conversationsSnapshot(): StoredConversation[] {
  const raw = storage().getItem(KEY) ?? "[]";
  if (raw !== snapshotRaw) {
    snapshotRaw = raw;
    snapshot = listConversations();
  }
  return snapshot;
}
