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

/** The left rail's collapsed state, published for the tab bar.
 *
 * `SidebarRail` owns the panel, but the control that brings a collapsed rail
 * back belongs in the tab bar beside the wordmark. The rail renders inside the
 * route and the tab bar above it, so they are siblings with no provider
 * between them to carry this.
 */
export type RailState = {
  /** True only while a mounted rail is collapsed to zero width. */
  collapsed: boolean;
  /** Collapses or expands the rail; null when no route has one mounted. */
  toggle: (() => void) | null;
};

const NO_RAIL: RailState = { collapsed: false, toggle: null };

let state: RailState = NO_RAIL;
const listeners = new Set<() => void>();

function emit(): void {
  for (const listener of listeners) listener();
}

/** Announces the rail's state. Compares by field, so the rail can call this
 * with a fresh object on every render without waking its readers. */
export function publishRailState(next: RailState): void {
  if (next.collapsed === state.collapsed && next.toggle === state.toggle)
    return;
  state = next;
  emit();
}

/** Withdraws a rail that is going away. Ignores a toggle that another route's
 * rail has already replaced, so navigating does not blank the control. */
export function retractRailState(toggle: () => void): void {
  if (state.toggle !== toggle) return;
  state = NO_RAIL;
  emit();
}

function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}

export function useRailState(): RailState {
  return useSyncExternalStore(
    subscribe,
    () => state,
    () => NO_RAIL,
  );
}
