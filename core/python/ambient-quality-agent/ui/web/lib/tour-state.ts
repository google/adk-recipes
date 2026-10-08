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

import { useSyncExternalStore } from "react";

/** Which step of the guided tour is open, if any.
 *
 * The tab bar's button opens the tour and `TourOverlay` draws it. Both sit
 * under the root route with no provider between them, so the state lives here,
 * as the rail's does in `rail-state`.
 */
let step: number | null = null;
const listeners = new Set<() => void>();

function publish(next: number | null): void {
  if (next === step) return;
  step = next;
  for (const listener of listeners) listener();
}

/** Opens the tour at its first step, restarting it when it is already open. */
export function startTour(): void {
  publish(0);
}

/** Shows the step at `index`, counted from 0. */
export function showTourStep(index: number): void {
  publish(index);
}

/** Closes the tour. */
export function endTour(): void {
  publish(null);
}

function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}

/** The open step's index, or null while the tour is closed. */
export function useTourStep(): number | null {
  return useSyncExternalStore(
    subscribe,
    () => step,
    () => null,
  );
}
