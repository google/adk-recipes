/**
 * Copyright 2026 Google LLC
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     https://www.apache.org/licenses/LICENSE-2.0
 *
 * Unless required by applicable law or agreed to in writing, software
 * distributed under the License is distributed on an "AS IS" BASIS,
 * WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 * See the License for the specific language governing permissions and
 * limitations under the License.
 */

// One studio (and its conversation with the agent) per browser, kept in localStorage.
const KEY = "genmedia.studioId";
// The server's studio ids (studio/store.py check_id): 1 to 57 characters, never `jobs`.
const VALID = /^(?!jobs$)[A-Za-z0-9_-]{1,57}$/;

/** A new studio id: 12 hex characters. */
export function newStudioId(): string {
  const id = Array.from(crypto.getRandomValues(new Uint8Array(6)), (b) =>
    b.toString(16).padStart(2, "0"),
  ).join("");
  localStorage.setItem(KEY, id);
  return id;
}

/** This browser's studio id, created on the first visit. */
export function loadStudioId(): string {
  const saved = localStorage.getItem(KEY);
  return saved && VALID.test(saved) ? saved : newStudioId();
}
