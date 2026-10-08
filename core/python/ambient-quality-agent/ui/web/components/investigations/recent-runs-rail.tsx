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

import { useQuery } from "@tanstack/react-query";
import { Link } from "@tanstack/react-router";
import { History } from "lucide-react";
import { runsQuery, type Run } from "@/lib/aqua-api";
import { count } from "@/lib/funnel";
import {
  ABSENT,
  isPastDue,
  runStateWord,
  renderScheduledText,
} from "@/lib/run-summary";
import { textStyle } from "@/lib/typography";
import { cn, formatSentenceCase } from "@/lib/utils";
import { SidebarSection } from "@/components/chat/sidebar-section";
import {
  SIDEBAR_META_CLASS,
  SIDEBAR_ROW_ACTIVE_CLASS,
  SIDEBAR_ROW_CLASS,
  SidebarAllLink,
  SidebarError,
  SidebarNote,
  SidebarSkeleton,
} from "@/components/chat/sidebar-states";
import {
  RAIL_SCROLL_CLASS,
  useRailScroll,
} from "@/components/chat/use-rail-scroll";

/** The last ten investigations, newest first, as a sidebar section.
 *
 * No count in the header: the server returns only the newest runs, so the
 * figure would be that page size rather than how many runs exist.
 */
export function RecentRunsRail() {
  // Same query as the list (one fetch); an empty or error line takes only its
  // own height.
  const { data, isError } = useQuery(runsQuery());
  const hasRows = data ? data.length > 0 : !isError;
  return (
    <SidebarSection
      title="Recent investigations"
      icon={History}
      storageKey="aqua.sidebar.runs"
      fill={hasRows}
      rightSlot={
        <SidebarAllLink to="/investigations" label="All investigations" />
      }
    >
      <RecentRunsList />
    </SidebarSection>
  );
}

function RecentRunsList() {
  // react-query keeps `data` through a failed refetch, so the rows stay up and
  // the error line is only for a list that never loaded.
  const { data, isError, refetch } = useQuery(runsQuery());
  const recent = data
    ? [...data].sort((a, b) => startedAt(b) - startedAt(a)).slice(0, 10)
    : [];
  const scrollRef = useRailScroll<HTMLUListElement>("runs");

  if (!data) {
    return isError ? (
      <SidebarError onRetry={() => void refetch()} />
    ) : (
      <SidebarSkeleton label="Loading investigations" rowClassName="h-7" />
    );
  }
  if (recent.length === 0)
    return <SidebarNote>No investigations yet.</SidebarNote>;

  return (
    <ul ref={scrollRef} className={cn(RAIL_SCROLL_CLASS, "flex flex-col pb-2")}>
      {recent.map((run) => (
        <li key={run.run_id}>
          <Link
            to="/investigations/$runId"
            params={{ runId: run.run_id }}
            // relative: the ratio's sr-only text is absolutely positioned, and
            // without a positioned row it escapes the section and stretches
            // the page on short windows.
            className={cn(
              SIDEBAR_ROW_CLASS,
              "group relative flex items-center gap-2 py-1",
            )}
            activeProps={{ className: SIDEBAR_ROW_ACTIVE_CLASS }}
          >
            <span className="min-w-0 flex-1 truncate">{when(run)}</span>
            <Outcome run={run} />
          </Link>
        </li>
      ))}
    </ul>
  );
}

/** Newest first by whichever start time the record carries; a run with none
 *  sorts last rather than breaking the sort. */
function startedAt(run: Run): number {
  const t = Date.parse(run.created_at || run.window_end || "");
  return Number.isFinite(t) ? t : -Infinity;
}

/** The share of judged trajectories that passed, as the runs table beside it
 *  writes it, or the run's state when it has no such share: "failed",
 *  "stalled" and "no data" are different from a low pass rate, so they are
 *  named rather than drawn. */
function Outcome({ run }: { run: Run }) {
  const state = runStateWord(run);
  if (state) {
    // The rail's slot is a badge, and a badge says "no data" for an
    // investigation that evaluated nothing; the full phrase is the tooltip.
    const word = state === "no trajectories evaluated" ? "no data" : state;
    const tone =
      word === "failed"
        ? "text-red-700 dark:text-red-400"
        : word === "stalled" || isPastDue(run)
          ? "text-amber-700 dark:text-amber-400"
          : null;
    return (
      <span
        title={
          word === state
            ? (renderScheduledText(run) ?? undefined)
            : "No trajectories evaluated"
        }
        className={cn(
          "shrink-0",
          // A toned word keeps its colour on a hovered or current row, which
          // the meta line's hover style would turn grey.
          tone ? cn(textStyle.meta, tone) : SIDEBAR_META_CLASS,
        )}
      >
        {formatSentenceCase(word)}
      </span>
    );
  }
  const passed = count(run.counters?.traces_eval_passed);
  const failed = count(run.counters?.traces_eval_failed);
  const judged = passed + failed;
  // Eval-service errors are no verdict on the agent, so a run of nothing else
  // has no share to draw.
  if (judged === 0) {
    return (
      <span
        title="Every evaluation hit an eval service error"
        className={SIDEBAR_META_CLASS}
      >
        {ABSENT}
      </span>
    );
  }
  // The same pass rate the runs table shows for the run, so the two agree.
  const ratio = `${passed.toLocaleString()} passed, ${failed.toLocaleString()} failed`;
  return (
    <>
      <span
        aria-hidden="true"
        className="h-1 w-7 shrink-0 overflow-hidden rounded-sm bg-muted-foreground/20"
      >
        <span
          className="block h-full bg-teal-600 dark:bg-teal-400"
          style={{ width: `${(passed / judged) * 100}%` }}
        />
      </span>
      <span
        title={ratio}
        className={cn(
          SIDEBAR_META_CLASS,
          "min-w-[3.25rem] shrink-0 text-right",
        )}
      >
        <span aria-hidden="true">{Math.round((passed / judged) * 100)}%</span>
        <span className="sr-only">{ratio}</span>
      </span>
    </>
  );
}

function when(run: Run): string {
  const t = startedAt(run);
  if (t === -Infinity) return "Undated";
  return new Date(t).toLocaleString(undefined, {
    month: "short",
    day: "numeric",
    hour: "numeric",
    minute: "2-digit",
  });
}
