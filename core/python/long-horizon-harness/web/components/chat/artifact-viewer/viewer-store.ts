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

export interface ArtifactInput {
  name: string;
  mimeType: string;
  /** base64 body, when the artifact travels inline in the FilePart. */
  bytes?: string;
  /** signed/download URL, when the artifact is fetched. */
  url?: string;
  /**
   * ``/workspace/...`` path when the tab points at a live workspace file
   * (opened from the sidebar tree) rather than a frozen artifact snapshot.
   * Only these tabs are editable.
   */
  path?: string;
}

export interface ArtifactTab extends ArtifactInput {
  id: string;
  /**
   * Bumped whenever the tab's contents may be stale (re-opened, or a turn
   * just ended). The viewer refetches a workspace file when this changes —
   * bytes alone can't tell us the agent rewrote the file underneath us.
   */
  revision: number;
}

export interface ViewerState {
  tabs: ArtifactTab[];
  activeId: string | null;
}

export const initialViewerState: ViewerState = { tabs: [], activeId: null };

// Identity is content-shaped so re-opening the same artifact activates the
// existing tab instead of stacking duplicates.
export function tabId(a: ArtifactInput): string {
  // A workspace file is identified by path alone: its bytes change on every
  // save, and a content-shaped id would stack a new tab per edit.
  if (a.path) return `ws:${a.path}`;
  return `${a.name}|${a.mimeType}|${a.bytes?.length ?? 0}|${a.url ?? ""}`;
}

export type ViewerAction =
  | { type: "open"; artifact: ArtifactInput }
  | { type: "reload"; id: string }
  | { type: "setBytes"; id: string; bytes: string }
  | { type: "close"; id: string }
  | { type: "activate"; id: string }
  | { type: "clear" };

export function viewerReducer(
  state: ViewerState,
  action: ViewerAction,
): ViewerState {
  switch (action.type) {
    case "open": {
      const id = tabId(action.artifact);
      const exists = state.tabs.some((t) => t.id === id);
      // Re-opening an already-open file is the user asking for it again —
      // usually because they expect it to have changed.
      const tabs = exists
        ? state.tabs.map((t) =>
            t.id === id ? { ...t, revision: t.revision + 1 } : t,
          )
        : [...state.tabs, { id, revision: 0, ...action.artifact }];
      return { tabs, activeId: id };
    }
    case "reload": {
      const tabs = state.tabs.map((t) =>
        t.id === action.id ? { ...t, revision: t.revision + 1 } : t,
      );
      return { ...state, tabs };
    }
    case "setBytes": {
      const tabs = state.tabs.map((t) =>
        t.id === action.id ? { ...t, bytes: action.bytes } : t,
      );
      return { ...state, tabs };
    }
    case "close": {
      const idx = state.tabs.findIndex((t) => t.id === action.id);
      if (idx === -1) return state;
      const tabs = state.tabs.filter((t) => t.id !== action.id);
      let activeId = state.activeId;
      if (state.activeId === action.id) {
        // Prefer the tab that slid into this slot, else the previous one.
        const neighbor = tabs[idx] ?? tabs[idx - 1] ?? null;
        activeId = neighbor ? neighbor.id : null;
      }
      return { tabs, activeId };
    }
    case "activate":
      return state.tabs.some((t) => t.id === action.id)
        ? { ...state, activeId: action.id }
        : state;
    case "clear":
      return initialViewerState;
  }
}
