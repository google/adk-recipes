"use client";
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

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useReducer,
  useRef,
  type ReactNode,
} from "react";
import type { SelectionAnchor } from "@/lib/selection-anchor";
import {
  initialViewerState,
  viewerReducer,
  type ArtifactInput,
  type ArtifactTab,
} from "./viewer-store";

/**
 * Text highlighted in the Source view, awaiting an instruction in the
 * composer.
 */
export interface PendingSelection {
  path: string;
  anchor: SelectionAnchor;
}

interface ViewerContextValue {
  tabs: ArtifactTab[];
  activeId: string | null;
  openArtifact: (artifact: ArtifactInput) => void;
  /**
   * Write-only. Forwards to the provider's `onSelectionChange`; the state
   * itself lives in ChatShell, which renders this provider and so cannot
   * consume its context.
   */
  setPendingSelection: (s: PendingSelection | null) => void;
  reloadTab: (id: string) => void;
  setBytes: (id: string, bytes: string) => void;
  closeTab: (id: string) => void;
  activateTab: (id: string) => void;
  clear: () => void;
}

const ViewerContext = createContext<ViewerContextValue | null>(null);

// `resetKey` (the active contextId) scopes open tabs to a conversation — tabs
// clear when the user switches chats.
export function ViewerProvider({
  children,
  resetKey,
  onOpen,
  onSelectionChange,
}: {
  children: ReactNode;
  resetKey?: string | null;
  onOpen?: () => void;
  onSelectionChange?: (s: PendingSelection | null) => void;
}) {
  const [state, dispatch] = useReducer(viewerReducer, initialViewerState);
  const onOpenRef = useRef(onOpen);
  onOpenRef.current = onOpen;
  const onSelectionRef = useRef(onSelectionChange);
  onSelectionRef.current = onSelectionChange;

  useEffect(() => {
    dispatch({ type: "clear" });
    // A selection scoped to the old chat's file must not leak into the next.
    onSelectionRef.current?.(null);
  }, [resetKey]);

  // dispatch is stable, so these are too. Consumers depend on them from
  // effects (autosave flushes on tab switch) — rebuilding them inside the
  // useMemo would give every viewer state change a new identity and fire
  // those effects spuriously.
  const openArtifact = useCallback((artifact: ArtifactInput) => {
    dispatch({ type: "open", artifact });
    onOpenRef.current?.();
  }, []);
  const setPendingSelection = useCallback(
    (s: PendingSelection | null) => onSelectionRef.current?.(s),
    [],
  );
  const reloadTab = useCallback(
    (id: string) => dispatch({ type: "reload", id }),
    [],
  );
  const setBytes = useCallback(
    (id: string, bytes: string) => dispatch({ type: "setBytes", id, bytes }),
    [],
  );
  const closeTab = useCallback(
    (id: string) => dispatch({ type: "close", id }),
    [],
  );
  const activateTab = useCallback(
    (id: string) => dispatch({ type: "activate", id }),
    [],
  );
  const clear = useCallback(() => dispatch({ type: "clear" }), []);

  const value = useMemo<ViewerContextValue>(
    () => ({
      tabs: state.tabs,
      activeId: state.activeId,
      openArtifact,
      setPendingSelection,
      reloadTab,
      setBytes,
      closeTab,
      activateTab,
      clear,
    }),
    [
      state,
      openArtifact,
      setPendingSelection,
      reloadTab,
      setBytes,
      closeTab,
      activateTab,
      clear,
    ],
  );

  return (
    <ViewerContext.Provider value={value}>{children}</ViewerContext.Provider>
  );
}

export function useViewer(): ViewerContextValue {
  const ctx = useContext(ViewerContext);
  if (!ctx) throw new Error("useViewer must be used within a ViewerProvider");
  return ctx;
}

// Non-throwing variant for components that render outside the provider (e.g.
// message bubbles in tests) — a no-op openArtifact keeps them safe.
export function useViewerOptional(): ViewerContextValue | null {
  return useContext(ViewerContext);
}
