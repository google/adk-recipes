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
 * The conversation a bare /c opens: the last one this browser had open for
 * the selected agent.
 *
 * A closed-then-reopened tab lands back on it, so useLhaTasks can spot a turn
 * still running there and useTaskResubscribe can reattach to it. Only a
 * conversation that exists is worth returning to: a new chat counts once
 * something has been sent in it, and a deleted one is forgotten. Each agent
 * has its own, so switching agents never reopens another agent's chat.
 */

const KEY = "lha:lastContextId";

/** The storage key for an agent's last chat. The default agent's is the
 *  unsuffixed key, beside the conversations that name no agent. */
function buildKey(agentKey?: string): string {
  return agentKey ? `${KEY}:${agentKey}` : KEY;
}

function runOnStorage<T>(op: (storage: Storage) => T): T | null {
  try {
    return typeof window === "undefined" ? null : op(window.localStorage);
  } catch {
    // Storage can throw outright in a locked-down or private context, and on
    // a write once it is full. Without a last chat, a bare /c opens a new one.
    return null;
  }
}

/** Returns the id of the conversation a bare /c opens for the selected
 *  agent, or null for none. */
export function readLastChat(agentKey?: string): string | null {
  return runOnStorage((s) => s.getItem(buildKey(agentKey)));
}

/** Makes `contextId` the conversation a bare /c opens for the agent. */
export function rememberLastChat(contextId: string, agentKey?: string): void {
  runOnStorage((s) => s.setItem(buildKey(agentKey), contextId));
}

/** Forgets the conversation a bare /c opens for the agent, so it opens a
 *  new chat. */
export function forgetLastChat(agentKey?: string): void {
  runOnStorage((s) => s.removeItem(buildKey(agentKey)));
}

/** Forgets a deleted conversation wherever it is some agent's last chat. */
export function forgetDeletedChat(contextId: string): void {
  runOnStorage((s) => {
    const keys: string[] = [];
    for (let i = 0; i < s.length; i++) {
      const key = s.key(i);
      if (key === KEY || key?.startsWith(`${KEY}:`)) keys.push(key);
    }
    for (const key of keys) {
      if (s.getItem(key) === contextId) s.removeItem(key);
    }
  });
}
