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

import { useState, type ReactNode } from "react";

/** Whether the reader last left the fold under `storageKey` open.
 *
 * @param storageKey The `localStorage` key the fold's state is kept under.
 * @returns `true` only when "1" is stored; `false` when nothing is stored or
 *   storage is unavailable (private mode).
 */
function isRememberedOpen(storageKey: string): boolean {
  try {
    return window.localStorage.getItem(storageKey) === "1";
  } catch {
    return false;
  }
}

/**
 * A `<details>` that opens the way the reader last left it, on every page that
 * uses the same `storageKey`.
 *
 * A reader who wants a fold open wants it open on the next page too; without
 * this they reopen it on every visit. Closed until first opened. Stored as
 * "1"/"0" like `SidebarSection`, and read on mount rather than in an effect so
 * the fold does not render closed and then jump open.
 */
export function RememberedDetails({
  storageKey,
  summary,
  summaryClassName,
  children,
}: {
  /** Folds that share a key share one remembered state. */
  storageKey: string;
  /** What the `<summary>` shows. */
  summary: ReactNode;
  summaryClassName?: string;
  /** The folded content. */
  children: ReactNode;
}) {
  const [open, setOpen] = useState(() => isRememberedOpen(storageKey));
  return (
    <details
      open={open}
      onToggle={(e) => {
        const next = e.currentTarget.open;
        setOpen(next);
        try {
          window.localStorage.setItem(storageKey, next ? "1" : "0");
        } catch {
          // Storage unavailable: the fold still opens, it just is not remembered.
        }
      }}
    >
      <summary className={summaryClassName}>{summary}</summary>
      {children}
    </details>
  );
}
