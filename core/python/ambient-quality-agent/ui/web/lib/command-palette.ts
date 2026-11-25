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

import { useEffect, useRef, useSyncExternalStore } from "react";
import type { LucideIcon } from "lucide-react";

/** The command palette's open state and the commands pages add to it.
 *
 * One palette is mounted under the root route, so it opens on every page. The
 * tab bar's button opens it, and a page such as the chat adds commands that
 * only make sense while it is shown. Neither sits above the other with a
 * provider between them, so the state lives here, as the rail's does in
 * `rail-state`.
 */
export interface PaletteCommand {
  id: string;
  label: string;
  /** A keyboard shortcut shown beside the label. */
  hint?: string;
  Icon: LucideIcon;
  /** `chats` lists the command after every other one, so a long list of chats
   *  does not push the commands out of view. */
  group?: "chats";
  /** Runs after the palette has closed. */
  onSelect: () => void;
}

let open = false;
const groups = new Map<symbol, readonly PaletteCommand[]>();
let commands: readonly PaletteCommand[] = [];
const listeners = new Set<() => void>();

function emit(): void {
  for (const listener of listeners) listener();
}

function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}

/** Opens the palette, or closes it.
 *
 * @param next - True to open, false to close.
 */
export function setCommandPaletteOpen(next: boolean): void {
  if (next === open) return;
  open = next;
  emit();
}

/** Opens the palette when it is closed and closes it when it is open. */
export function toggleCommandPalette(): void {
  setCommandPaletteOpen(!open);
}

/** Returns true while the palette is open, for event handlers that read it
 *  outside render. */
export function isCommandPaletteOpen(): boolean {
  return open;
}

/** Returns whether the palette is open, re-rendering when that changes. */
export function useCommandPaletteOpen(): boolean {
  return useSyncExternalStore(
    subscribe,
    () => open,
    () => false,
  );
}

/** Returns the commands every mounted page has added, in mount order. */
export function usePaletteCommands(): readonly PaletteCommand[] {
  return useSyncExternalStore(
    subscribe,
    () => commands,
    () => commands,
  );
}

/** Adds commands to the palette for as long as the caller is mounted.
 *
 * Pass a memoized array: a new one on every render re-registers it each time.
 *
 * @param pageCommands - The commands to add.
 */
export function useRegisterPaletteCommands(
  pageCommands: readonly PaletteCommand[],
): void {
  // One key for the caller's whole life, so an update replaces its commands in
  // place rather than moving them to the end of the mount order.
  const keyRef = useRef<symbol | null>(null);
  if (!keyRef.current) keyRef.current = Symbol("palette-commands");
  const key = keyRef.current;

  useEffect(() => {
    groups.set(key, pageCommands);
    commands = [...groups.values()].flat();
    emit();
  }, [key, pageCommands]);

  useEffect(
    () => () => {
      groups.delete(key);
      commands = [...groups.values()].flat();
      emit();
    },
    [key],
  );
}

/** Returns the palette's shortcut as this platform writes it: `⌘K` on Apple
 *  devices, `Ctrl+K` elsewhere. */
export function formatPaletteShortcut(): string {
  const apple =
    typeof navigator !== "undefined" &&
    /Mac|iPhone|iPad/.test(navigator.userAgent);
  return apple ? "⌘K" : "Ctrl+K";
}
