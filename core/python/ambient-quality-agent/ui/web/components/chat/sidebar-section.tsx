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

import { useCallback, useEffect, useId, useState, type ReactNode } from "react";
import { ChevronRight, type LucideIcon } from "lucide-react";
import { textStyle } from "@/lib/typography";
import { cn } from "@/lib/utils";

interface SidebarSectionProps {
  title: string;
  icon: LucideIcon;
  storageKey: string;
  /** Shown as a pill beside the title; omitted when zero or unknown. */
  count?: number;
  defaultOpen?: boolean;
  /** Controls on the header's right, before the chevron — "All", delete. */
  rightSlot?: ReactNode;
  /** Extra classes for the header row — e.g. `pr-12` to clear an overlaid close button. */
  headerClassName?: string;
  /** Replaces the open section's default floor, `min-h-[5.5rem]` (a header
   *  and about two rows), for a body that needs more before a row shows. */
  floorClassName?: string;
  /** False when the body is a one-line note (empty, error): the section is
   *  then only as tall as that line instead of taking a share of the rail. */
  fill?: boolean;
  children: ReactNode;
}

export function SidebarSection({
  title,
  icon: Icon,
  storageKey,
  count,
  defaultOpen = true,
  rightSlot,
  headerClassName,
  floorClassName = "min-h-[5.5rem]",
  fill = true,
  children,
}: SidebarSectionProps) {
  const [open, setOpen] = useState(defaultOpen);
  const bodyId = useId();

  useEffect(() => {
    if (typeof window === "undefined") return;
    let raw: string | null = null;
    try {
      raw = window.localStorage.getItem(storageKey);
    } catch {
      // Storage access denied: keep the default rather than crash the app.
    }
    if (raw === "1") setOpen(true);
    else if (raw === "0") setOpen(false);
  }, [storageKey]);

  const toggle = useCallback(() => {
    const next = !open;
    setOpen(next);
    try {
      window.localStorage.setItem(storageKey, next ? "1" : "0");
    } catch {
      // localStorage unavailable (private mode); state still flips
    }
  }, [open, storageKey]);

  return (
    // Open sections split the rail equally, each capped at its own content,
    // so a short section hands the rest of its share to the others. The
    // floor keeps a header and about two rows once the rail runs out of
    // height (small windows, 400% zoom): below it the rail itself scrolls
    // instead of sections shrinking under their headers and overlapping.
    // A one-line body (`fill={false}`) stays out of the split: the floor would
    // leave a pocket under it, and Firefox, which treats block-axis
    // max-content as none, would stretch it to a full third. Cap verified in
    // Chrome and WebKit; in Firefox sections with rows degrade to equal
    // thirds (blank pockets, never overlap).
    <section
      aria-labelledby={`${bodyId}-title`}
      className={cn(
        "flex flex-col border-t first:border-t-0",
        open && fill
          ? cn("flex-1 max-h-max overflow-hidden", floorClassName)
          : "shrink-0",
      )}
    >
      {/* The toggle's ::after stretches over the whole header, so the row is
          one click target while "All" and the other actions stay siblings
          of the button (raised above the overlay) rather than nested in it. */}
      <div
        className={cn(
          "relative flex h-9 shrink-0 items-center gap-1 pl-4 pr-2 transition-colors hover:bg-accent/50",
          headerClassName,
        )}
      >
        <h2 className="flex min-w-0 flex-1">
          <button
            type="button"
            id={`${bodyId}-title`}
            onClick={toggle}
            aria-expanded={open}
            aria-controls={open ? bodyId : undefined}
            className="flex min-w-0 flex-1 items-center gap-2 text-left after:absolute after:inset-0 focus-visible:outline-none focus-visible:after:ring-2 focus-visible:after:ring-inset focus-visible:after:ring-ring"
          >
            <Icon
              aria-hidden="true"
              className="h-3.5 w-3.5 shrink-0 text-muted-foreground"
            />
            <span className={cn(textStyle.itemTitle, "truncate")}>{title}</span>
            {typeof count === "number" && count > 0 && (
              <span
                className={cn(
                  textStyle.meta,
                  "shrink-0 rounded-full bg-muted px-1.5",
                )}
              >
                {count}
              </span>
            )}
          </button>
        </h2>
        {rightSlot && (
          <div className="relative z-10 flex shrink-0 items-center">
            {rightSlot}
          </div>
        )}
        {/* pointer-events-none: once rotated, the chevron is transformed and
            paints above the toggle's overlay, so it would swallow the click. */}
        <ChevronRight
          aria-hidden="true"
          className={cn(
            "pointer-events-none h-3.5 w-3.5 shrink-0 text-muted-foreground motion-safe:transition-transform",
            open && "rotate-90",
          )}
        />
      </div>
      {open && (
        <div id={bodyId} className="flex min-h-0 flex-1 flex-col">
          {children}
        </div>
      )}
    </section>
  );
}
