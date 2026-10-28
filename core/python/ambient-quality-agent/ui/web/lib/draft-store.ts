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

const KEY = "horizon.drafts.v1";
const CAP = 50;

type Drafts = Record<string, string>;

function read(): Drafts {
  try {
    const raw = localStorage.getItem(KEY);
    return raw ? (JSON.parse(raw) as Drafts) : {};
  } catch {
    return {};
  }
}

function write(d: Drafts): void {
  try {
    localStorage.setItem(KEY, JSON.stringify(d));
  } catch {
    // storage full / unavailable — drafts are best-effort
  }
}

export function loadDraft(contextId: string | undefined): string {
  if (!contextId) return "";
  return read()[contextId] ?? "";
}

export function saveDraft(contextId: string | undefined, text: string): void {
  if (!contextId) return;
  const d = read();
  if (text.trim().length === 0) {
    delete d[contextId];
  } else {
    // Re-insert last so JSON key order acts as an LRU recency list.
    delete d[contextId];
    d[contextId] = text;
    const keys = Object.keys(d);
    if (keys.length > CAP)
      for (const k of keys.slice(0, keys.length - CAP)) delete d[k];
  }
  write(d);
}

export function clearDraft(contextId: string | undefined): void {
  if (!contextId) return;
  const d = read();
  delete d[contextId];
  write(d);
}
