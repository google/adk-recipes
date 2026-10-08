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
 * The conversation a bare /c opens: the last one this browser had open.
 *
 * A closed-then-reopened tab lands back on it, so useLhaTasks can spot a turn
 * still running there and useTaskResubscribe can reattach to it. Only a
 * conversation that exists is worth returning to: a new chat counts once
 * something has been sent in it, and a deleted one is forgotten.
 */

const KEY = "lha:lastContextId";

function runOnStorage<T>(op: (storage: Storage) => T): T | null {
  try {
    return typeof window === "undefined" ? null : op(window.localStorage);
  } catch {
    // Storage can throw outright in a locked-down or private context, and on
    // a write once it is full. Without a last chat, a bare /c opens a new one.
    return null;
  }
}

/** Returns the id of the conversation a bare /c opens, or null for none. */
export function readLastChat(): string | null {
  return runOnStorage((s) => s.getItem(KEY));
}

/** Makes `contextId` the conversation a bare /c opens. */
export function rememberLastChat(contextId: string): void {
  runOnStorage((s) => s.setItem(KEY, contextId));
}

/**
 * Forgets the conversation a bare /c opens, so it opens a new chat. Given a
 * `contextId`, forgets it only if it is that conversation.
 */
export function forgetLastChat(contextId?: string): void {
  runOnStorage((s) => {
    if (contextId === undefined || s.getItem(KEY) === contextId) {
      s.removeItem(KEY);
    }
  });
}
