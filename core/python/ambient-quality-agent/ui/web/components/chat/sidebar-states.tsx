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

/** The pieces the sidebar's three lists share, so Chats, Top insights and Recent
 *  investigations read as one component: row styling, the loading, empty and
 *  error lines, and the header's "All" link. */

import type { ReactNode } from "react";
import { Link } from "@tanstack/react-router";
import { ArrowRight } from "lucide-react";
import { link, textStyle } from "@/lib/typography";
import { cn } from "@/lib/utils";

/** A list row. Text starts 16px from the rail edge: the list's `px-2` plus
 *  this `px-2`. The focus ring is inset so a scrolling list cannot clip it. */
export const SIDEBAR_ROW_CLASS =
  `${textStyle.body} rounded-md px-2 transition-colors hover:bg-accent ` +
  "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-ring";

/** The row for the current route: background and weight only. A side bar drawn
 *  as an inset shadow bends around the rounded corners into a bracket. */
export const SIDEBAR_ROW_ACTIVE_CLASS = "bg-accent font-medium text-foreground";

/** A row's secondary line. Muted grey is 4.4:1 on the light accent, just short
 *  of AA, so a hovered or current row darkens it. */
export const SIDEBAR_META_CLASS =
  `${textStyle.meta} ` +
  "group-hover:text-foreground/70 group-aria-[current=page]:text-foreground/70";

/** Counts as "715", "9,999", "12K" or "1.2M" — wide numbers otherwise push a
 *  row's other text out of a 260px rail. */
export function formatCount(n: number): string {
  return n >= 10_000
    ? new Intl.NumberFormat(undefined, {
        notation: "compact",
        maximumFractionDigits: 1,
      }).format(n)
    : n.toLocaleString();
}

/** Placeholder rows while a list loads, three rows tall so the section holds
 *  its share of the rail instead of growing, and shoving its neighbours, when
 *  the rows arrive. */
export function SidebarSkeleton({
  label,
  rowClassName = "h-8",
}: {
  /** What is loading, for assistive tech: "Loading chats". */
  label: string;
  rowClassName?: string;
}) {
  return (
    <div
      role="status"
      aria-label={label}
      className="flex flex-col gap-1 px-2 py-1"
    >
      <span className="sr-only">{label}…</span>
      {Array.from({ length: 3 }).map((_, i) => (
        <div
          // biome-ignore lint/suspicious/noArrayIndexKey: the placeholder rows are a fixed count with nothing to tell them apart.
          key={i}
          aria-hidden="true"
          className={cn(
            "motion-safe:animate-pulse rounded-md bg-muted",
            rowClassName,
          )}
        />
      ))}
    </div>
  );
}

/** A section's one-line state: empty, or failed with a retry. */
export function SidebarNote({ children }: { children: ReactNode }) {
  return <p className={cn(textStyle.meta, "px-4 py-1.5")}>{children}</p>;
}

/** The line a list shows when it failed and has nothing cached to show. */
export function SidebarError({ onRetry }: { onRetry: () => void }) {
  return (
    <SidebarNote>
      Couldn&rsquo;t load —{" "}
      <button
        type="button"
        onClick={onRetry}
        className={cn(link.standalone, "font-medium text-foreground")}
      >
        Retry
      </button>
    </SidebarNote>
  );
}

/** The header's "All" link to the full page a list samples. */
export function SidebarAllLink({
  to,
  label,
}: {
  to: "/insights" | "/investigations";
  /** The accessible name: "All insights". */
  label: string;
}) {
  return (
    <Link
      to={to}
      aria-label={label}
      title={label}
      activeOptions={{ exact: true }}
      className={cn(
        textStyle.meta,
        "inline-flex h-6 items-center gap-0.5 rounded-md px-1.5 transition-colors hover:text-primary focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring",
      )}
    >
      All
      <ArrowRight className="h-3 w-3" />
    </Link>
  );
}
