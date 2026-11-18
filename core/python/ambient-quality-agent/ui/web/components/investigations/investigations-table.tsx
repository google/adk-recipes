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

import { Link, useNavigate, useRouter } from "@tanstack/react-router";
import { type RefObject, useLayoutEffect, useRef, useState } from "react";
import type { InvestigationListItem } from "@/lib/aqua-api";
import { describeFailure, isNoData } from "@/lib/failure";
import { count, STAGES, type StageId } from "@/lib/funnel";
import {
  ABSENT,
  agentLabel,
  describeProgress,
  extractCurrentProgress,
  extractReviewFocus,
  formatInstant,
  isAmbient,
  isPastDue,
  passRate,
  formatInvestigationStateWord,
  buildInvestigationStats,
  renderScheduledText,
  telemetryWindow,
  windowLength,
} from "@/lib/investigation-summary";
import {
  inFlightAge,
  isStalled,
  listInFlightInvestigations,
  newestFirst,
  startedAt,
} from "@/lib/investigation-state";
import { link, mono, textStyle } from "@/lib/typography";
import { cn, formatSentenceCase } from "@/lib/utils";
import { ProgressBar } from "./investigation-progress";
import { TriggerBadge } from "./trigger-badge";

/** Padding for every cell, header included, so the columns line up. */
const CELL = "px-3 py-2 first:pl-4 last:pr-4";

/** Red for bad news, in shades that stay legible as small text on either
 *  theme's background. */
const RED = "text-red-600 dark:text-red-400";

/** Rising in the passed colour, in a shade legible as small text. */
const TEAL = "text-teal-700 dark:text-teal-400";

/** One segment of a funnel stage, for its colour and name. */
function segment(stage: StageId, key: string) {
  // biome-ignore lint/style/noNonNullAssertion: called only at module load with stages and keys that STAGES defines.
  return STAGES.find((s) => s.id === stage)!.segments.find(
    (seg) => seg.key === key,
  )!;
}

const WINDOW_SEGMENT = segment("window", "trajectories_scanned");
const PASSED_SEGMENT = segment("evaluated", "trajectories_eval_passed");
const FAILED_SEGMENT = segment("evaluated", "trajectories_eval_failed");

/** The counts a row gives bare, one right-aligned column each, in the funnel's
 *  colours: eval-service errors, then new and recurring insights. Named rather
 *  than taken whole from the funnel's stages, which can gain segments that no
 *  single investigation counts. */
const COUNT_COLUMNS = [
  { ...segment("evaluated", "trajectories_eval_errored"), label: "errors" },
  segment("insights", "insights_created"),
  segment("insights", "insights_recurring"),
];

/**
 * Investigations as a table, one row each, in the order given: when each
 * started, the trajectories in its window, its pass rate and how that moved
 * since the sweep before, its eval-service errors, and the new and recurring
 * insights it recorded. Every figure has a column of its own, right-aligned,
 * so the figures line up down the list. Clicking a row opens the
 * investigation.
 *
 * A row says only what sets it apart. A value every row would repeat is left
 * out: the agent, which the tab bar also names, unless the investigations
 * observed more than one; and the window, unless this investigation's differs
 * from the one most of the others share. The start time's tooltip gives the
 * window either way.
 *
 * An investigation somebody started, custom or manual, carries a badge saying
 * so, and a custom investigation the start of the focus it was asked to review:
 * those are the investigations a user is waiting on, among dozens of sweeps. A
 * sweep carries none, being what most rows are.
 *
 * Only ambient sweeps are compared: the change in pass rate and the length of
 * the traffic bar are measured against other sweeps, never against a manual or
 * custom investigation that chose its own window or conversations.
 *
 * An investigation still going has no outcome to tabulate, so it is drawn
 * apart, in a row of its own above the table, with how far it got; the table
 * holds the rest, stalled ones included, and is left out when nothing has
 * settled.
 *
 * Home's recent investigations and the investigations page both draw their
 * investigations with this, so the two read alike.
 */
export function InvestigationsTable({
  investigations,
  history = investigations,
}: {
  investigations: readonly InvestigationListItem[];
  /** The whole list the rows were cut or filtered from. Each row's previous
   *  investigation is looked for in it, so the last row of five, or the row
   *  after a hidden one, still has one to compare with; and the traffic bars
   *  are scaled to its busiest sweep, so a row's bar keeps its length when the
   *  rows are filtered. */
  history?: readonly InvestigationListItem[];
}) {
  const navigate = useNavigate();
  const router = useRouter();
  const live = listInFlightInvestigations(investigations);
  const settled = investigations.filter((i) => !live.includes(i));
  const showAgent = new Set(investigations.map(agentLabel)).size > 1;
  const usualWindow = mostCommon(settled.map(windowNote));
  const busiest = Math.max(
    0,
    ...history
      .filter(isAmbient)
      .map((investigation) =>
        count(investigation.counters?.trajectories_scanned),
      ),
  );
  const previousRates = previousPassRates(history);
  const liveRef = useRef<HTMLDivElement>(null);
  const tableRef = useRef<HTMLDivElement>(null);
  const { leadWidth, startedMinWidth } = useStageColumnUnderWindowBar(
    liveRef,
    tableRef,
    live.length > 0 && settled.length > 0,
  );
  return (
    <div className="flex flex-col gap-2">
      {live.length > 0 && (
        // One grid for every running investigation, its rows taking their
        // columns from it, so stages, bars and counts line up row to row.
        <div
          ref={liveRef}
          className="scroll-x-edges grid grid-cols-[auto_auto_minmax(6rem,1fr)_auto] divide-y overflow-x-auto rounded-lg border"
          style={
            leadWidth === undefined
              ? undefined
              : {
                  gridTemplateColumns: `${leadWidth}px auto minmax(6rem, 1fr) auto`,
                }
          }
        >
          {live.map((investigation) => (
            <InFlightRow
              key={investigation.investigation_id}
              investigation={investigation}
              showAgent={showAgent}
            />
          ))}
        </div>
      )}
      {settled.length > 0 && (
        // A phone still scrolls it sideways: scroll-x-edges shades the side that
        // has more, so the columns past the edge are not a secret.
        <div
          ref={tableRef}
          className="scroll-x-edges overflow-x-auto rounded-lg border"
        >
          <table className={cn(textStyle.body, "w-full text-left")}>
            <thead className={textStyle.label}>
              <tr className="border-b">
                <th
                  className={cn(CELL, "font-medium")}
                  style={
                    startedMinWidth === undefined
                      ? undefined
                      : { minWidth: startedMinWidth }
                  }
                >
                  Started
                </th>
                {showAgent && (
                  <th className={cn(CELL, "font-medium")}>Agent</th>
                )}
                {/* Left out below a full page, where the other figures need
                  the room more. */}
                <th
                  className={cn(
                    CELL,
                    "hidden text-right font-medium lg:table-cell",
                  )}
                >
                  Trajectories
                </th>
                {/* The widest once there is room for the bar, long enough to
                  compare by eye. */}
                <th className={cn(CELL, "text-right font-medium xl:w-1/4")}>
                  Pass rate
                </th>
                <th className={cn(CELL, "text-right font-medium")}>Change</th>
                {COUNT_COLUMNS.map((col) => (
                  <th
                    key={col.key}
                    className={cn(CELL, "text-right font-medium")}
                  >
                    <span className="inline-flex items-center gap-1.5">
                      <span
                        aria-hidden="true"
                        className={cn("h-2 w-2 rounded-full", col.tone)}
                      />
                      {formatSentenceCase(col.label)}
                    </span>
                  </th>
                ))}
              </tr>
            </thead>
            <tbody className="divide-y">
              {settled.map((investigation) => {
                const note = windowNote(investigation);
                const bounds = telemetryWindow(investigation);
                const focus = extractReviewFocus(investigation);
                const href = router.buildLocation({
                  to: "/investigations/$investigationId",
                  params: { investigationId: investigation.investigation_id },
                }).href;
                return (
                  <tr
                    key={investigation.investigation_id}
                    // The investigation id is the row's link, for the keyboard and
                    // a screen reader. A click elsewhere on the row goes to the
                    // same page, unless it ends a text selection; with a
                    // modifier, or the middle button, it opens a new tab, as
                    // a click on a link would, and the middle press does not
                    // start the browser's autoscroll first.
                    onClick={(e) => {
                      if (
                        (e.target as Element).closest("a") ||
                        window.getSelection()?.toString()
                      ) {
                        return;
                      }
                      if (e.metaKey || e.ctrlKey || e.shiftKey) {
                        window.open(href, "_blank", "noopener");
                        return;
                      }
                      void navigate({
                        to: "/investigations/$investigationId",
                        params: {
                          investigationId: investigation.investigation_id,
                        },
                      });
                    }}
                    onMouseDown={(e) => {
                      if (
                        e.button === 1 &&
                        !(e.target as Element).closest("a")
                      ) {
                        e.preventDefault();
                      }
                    }}
                    onAuxClick={(e) => {
                      if (
                        e.button === 1 &&
                        !(e.target as Element).closest("a")
                      ) {
                        window.open(href, "_blank", "noopener");
                      }
                    }}
                    className="cursor-pointer hover:bg-muted/30 focus-within:bg-muted/30"
                  >
                    {/* No cell is kept to one line: in a narrow panel a row
                    wraps rather than pushing a column out of sight. */}
                    <td className={CELL}>
                      <span className="flex flex-wrap items-baseline gap-x-2 gap-y-0.5">
                        <span
                          className="whitespace-nowrap"
                          title={
                            bounds === ABSENT ? undefined : `Window ${bounds}`
                          }
                        >
                          {startedLabel(investigation)}
                        </span>
                        <Link
                          to="/investigations/$investigationId"
                          params={{
                            investigationId: investigation.investigation_id,
                          }}
                          className={cn(textStyle.meta, mono, link.standalone)}
                        >
                          {investigation.investigation_id.slice(0, 8)}
                        </Link>
                        {!isAmbient(investigation) && (
                          <TriggerBadge investigation={investigation} />
                        )}
                        {note !== usualWindow && note !== ABSENT && (
                          <span
                            className={cn(
                              textStyle.meta,
                              "whitespace-nowrap rounded border px-1.5",
                            )}
                          >
                            {note}
                          </span>
                        )}
                      </span>
                      {focus && (
                        <span
                          className={cn(textStyle.meta, "mt-0.5 line-clamp-1")}
                          title={focus}
                        >
                          {focus}
                        </span>
                      )}
                    </td>
                    {showAgent && (
                      <td className={cn(CELL, textStyle.meta)}>
                        {agentLabel(investigation)}
                      </td>
                    )}
                    <td className={cn(CELL, "hidden text-right lg:table-cell")}>
                      <TrajectoriesScanned
                        investigation={investigation}
                        busiest={busiest}
                      />
                    </td>
                    <td className={cn(CELL, "text-right")}>
                      <InvestigationPassRate investigation={investigation} />
                    </td>
                    <td className={cn(CELL, "text-right")}>
                      <InvestigationChange
                        rate={passRate(investigation)}
                        previous={previousRates.get(
                          investigation.investigation_id,
                        )}
                      />
                    </td>
                    <InvestigationCountCells investigation={investigation} />
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

/** An investigation still going, as a row of the block above the table. It
 *  opens with what the table's Started cell holds (when it started, its id,
 *  its trigger badge), then a dot and the stage it is in, its bar, and its
 *  counts so far in words: "Session review ▕███░░░░░▏ 45 of 205
 *  trajectories, 12 findings". Before its first report the stage reads
 *  "Running" or "Pending". The dot pulses while it runs, the row fades in as
 *  the investigation starts, and the whole row opens the investigation. The
 *  bar is hidden from assistive technology, which reads the state and the
 *  counts in words instead. The progress comes with the list, so the row
 *  moves whenever the list is re-read. */
function InFlightRow({
  investigation,
  showAgent,
}: {
  investigation: InvestigationListItem;
  showAgent: boolean;
}) {
  const progress = extractCurrentProgress(investigation);
  const state = formatInvestigationStateWord(investigation) ?? "";
  return (
    <Link
      to="/investigations/$investigationId"
      params={{ investigationId: investigation.investigation_id }}
      className="col-span-full grid grid-cols-subgrid items-center gap-x-4 px-4 py-2 hover:bg-muted/30 focus-visible:bg-muted/30 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-ring motion-safe:animate-fade-in-up"
    >
      <span
        data-live-lead
        className="flex items-baseline gap-2 justify-self-start whitespace-nowrap"
      >
        <span>{startedLabel(investigation)}</span>
        <span className={cn(textStyle.meta, mono)}>
          {investigation.investigation_id.slice(0, 8)}
        </span>
        {!isAmbient(investigation) && (
          <TriggerBadge investigation={investigation} />
        )}
        {showAgent && (
          <span className={textStyle.meta}>{agentLabel(investigation)}</span>
        )}
      </span>
      <span
        className={cn(
          textStyle.meta,
          "inline-flex items-center gap-2 whitespace-nowrap text-foreground",
        )}
      >
        <span
          aria-hidden="true"
          data-live-dot
          className={cn(
            "h-1.5 w-1.5 shrink-0 rounded-full",
            state === "running"
              ? "bg-muted-foreground motion-safe:animate-pulse"
              : "bg-muted-foreground/50",
          )}
        />
        {progress ? (
          <>
            <span className="sr-only">{formatSentenceCase(state)}: </span>
            {progress.stage}
          </>
        ) : (
          formatSentenceCase(state)
        )}
      </span>
      <span aria-hidden="true" className="flex">
        {progress && (
          <ProgressBar progress={progress} className="h-1.5 w-full" />
        )}
      </span>
      <span className={cn(textStyle.meta, "whitespace-nowrap text-right")}>
        {progress ? describeProgress(progress) : "No progress reported yet"}
      </span>
    </Link>
  );
}

interface StageColumnLayout {
  leadWidth?: number;
  startedMinWidth?: number;
}

/** Widths that put the running rows' stage dots straight above the table's
 *  trajectories bars. The two blocks are laid out apart, and the table sizes
 *  its own columns, so this measures where the first shown bar starts and
 *  makes the running rows' first column end there. When that column needs
 *  more room than the table leaves before the bar, the table's Started column
 *  is widened until it does, and let go once it is wider than it needs; the
 *  observer re-measures after each change, so the two settle together.
 *  Positions are taken within each block, scrolled sideways or not. Without a
 *  shown bar (below a wide page, where it is hidden, or with no settled rows)
 *  both keep their own layout. `present` says whether both blocks are on the
 *  page, so the observer is attached when they are. */
function useStageColumnUnderWindowBar(
  liveRef: RefObject<HTMLDivElement | null>,
  tableRef: RefObject<HTMLDivElement | null>,
  present: boolean,
): StageColumnLayout {
  const [layout, setLayout] = useState<StageColumnLayout>({});
  const measure = useRef(() => {});
  measure.current = () => {
    const live = liveRef.current;
    const table = tableRef.current;
    const bar = Array.from(
      table?.querySelectorAll<HTMLElement>("[data-window-bar]") ?? [],
    ).find((element) => element.offsetWidth > 0);
    const leads = Array.from(
      live?.querySelectorAll<HTMLElement>("[data-live-lead]") ?? [],
    );
    const started = table?.querySelector("th");
    const dot = live?.querySelector<HTMLElement>("[data-live-dot]");
    const stage = dot?.parentElement;
    // The rows borrow the block's columns with their own padding and gap,
    // which offset the content from the column lines, so rather than add
    // those up this moves the first column by however far the dot is from
    // the bar now. The browser reports the columns it resolved.
    const current = live
      ? Number.parseFloat(getComputedStyle(live).gridTemplateColumns)
      : Number.NaN;
    if (
      !live ||
      !table ||
      !bar ||
      !started ||
      !dot ||
      !stage ||
      leads.length === 0 ||
      !Number.isFinite(current)
    ) {
      setLayout((prev) =>
        prev.leadWidth === undefined && prev.startedMinWidth === undefined
          ? prev
          : {},
      );
      return;
    }
    const within = (block: HTMLElement, edge: number) =>
      edge - block.getBoundingClientRect().left + block.scrollLeft;
    const left = (element: Element, block: HTMLElement) =>
      within(block, element.getBoundingClientRect().left);
    const gap =
      Number.parseFloat(
        getComputedStyle(stage.parentElement ?? stage).columnGap,
      ) || 0;
    // From the content, not the cell: the cell is already held to the width
    // set last time, and its content may spill past it.
    const contentRight = Math.max(
      ...leads.map((lead) => {
        const last = lead.lastElementChild ?? lead;
        return within(live, last.getBoundingClientRect().right);
      }),
    );
    const target = current + left(bar, table) - left(dot, live);
    const least = current + contentRight + gap - left(stage, live);
    const short = least - target;
    setLayout((prev) => {
      let startedMinWidth = prev.startedMinWidth;
      if (short > 0.5) {
        startedMinWidth = started.getBoundingClientRect().width + short;
      } else if (short < -0.5) {
        // Wider than the running rows need: let the table size the column
        // again, and widen it afresh if that leaves too little.
        startedMinWidth = undefined;
      }
      const leadWidth = Math.max(target, least);
      const same =
        prev.startedMinWidth === startedMinWidth &&
        prev.leadWidth !== undefined &&
        Math.abs(prev.leadWidth - leadWidth) < 0.5;
      return same ? prev : { leadWidth, startedMinWidth };
    });
  };
  // Every render, since a poll can change a row's lead or the bar's place.
  useLayoutEffect(() => measure.current());
  useLayoutEffect(() => {
    const live = liveRef.current;
    const table = tableRef.current;
    if (!present || !live || !table || typeof ResizeObserver === "undefined") {
      return;
    }
    const observer = new ResizeObserver(() => measure.current());
    observer.observe(live);
    observer.observe(table);
    const started = table.querySelector("th");
    if (started) observer.observe(started);
    return () => observer.disconnect();
  }, [present, liveRef, tableRef]);
  return layout;
}

/** The value most of `values` share, or undefined when no two share one. A
 *  single value is its own majority. */
function mostCommon(values: readonly string[]): string | undefined {
  const tally = new Map<string, number>();
  for (const value of values) tally.set(value, (tally.get(value) ?? 0) + 1);
  let best: string | undefined;
  let bestCount = values.length === 1 ? 0 : 1;
  for (const [value, n] of tally) {
    if (n > bestCount) [best, bestCount] = [value, n];
  }
  return best;
}

/** The window as a row notes it when it differs from the others': "30h
 *  window", or its bounds when it did not end where the investigation
 *  started. */
function windowNote(investigation: InvestigationListItem): string {
  const length = windowLength(investigation);
  return length ? `${length} window` : telemetryWindow(investigation);
}

/** "Sep 24, 8:00 AM", by whichever start time the record carries. */
function startedLabel(investigation: InvestigationListItem): string {
  const t = startedAt(investigation);
  return t === null ? "Undated" : formatInstant(new Date(t).toISOString());
}

/** An investigation's outcome: its pass rate, "74%" of the trajectories judged
 *  (eval-service errors are not a judgement), or its failure/in-flight status.
 *  A dash when every evaluation errored. */
export function formatInvestigationOutcome(
  investigation: InvestigationListItem,
): string {
  const state = formatInvestigationStateWord(investigation);
  if (state) return state;
  const rate = passRate(investigation);
  return rate === null ? ABSENT : percent(rate);
}

/** The colour `formatInvestigationOutcome` is written in: red for a genuine
 *  failure, amber for a stalled investigation or a scheduled one past its due
 *  time, muted for the rest. A sweep that failed for want of telemetry is muted
 *  too: a column of red on a deployment nobody has used yet is the wrong thing
 *  to tell whoever just set it up. */
export function resolveInvestigationOutcomeTone(
  investigation: InvestigationListItem,
): string {
  const status = (investigation.status || "").toLowerCase();
  if (status === "skipped" || status === "cancelled")
    return "text-muted-foreground";
  if (investigation.error) {
    return isNoData(investigation.error) ? "text-muted-foreground" : RED;
  }
  if (status === "failed") return RED;
  if (isStalled(investigation) || isPastDue(investigation)) {
    return "text-amber-700 dark:text-amber-400";
  }
  return "text-muted-foreground";
}

/** Why an investigation is in the state its row names: the error it failed
 *  with, what a failure for want of telemetry means, when a scheduled
 *  investigation starts, or how long a stalled investigation has gone without
 *  an update. Undefined when the state explains itself. */
function stateHint(investigation: InvestigationListItem): string | undefined {
  if (investigation.error) {
    const failure = describeFailure(investigation.error);
    return failure.kind === "no-data"
      ? (failure.hint ?? failure.cause)
      : investigation.error;
  }
  const scheduled = renderScheduledText(investigation);
  if (scheduled) return scheduled;
  if (!isStalled(investigation)) return undefined;
  // The row says "stalled" while the record says running, and Run
  // investigation is available again: the reader is owed the reason.
  const status = (investigation.status || "in flight").toLowerCase();
  return (
    `No update for ${inFlightAge(investigation)}. The record still says ${status}, ` +
    `but a new investigation can be started.`
  );
}

/** An investigation's counters, keyed as the funnel's segments are: they are
 *  named after the totals, which sum these same counters. */
function countersOf(
  investigation: InvestigationListItem,
): Record<string, number | undefined> {
  return (investigation.counters ?? {}) as Record<string, number | undefined>;
}

/** Each sweep's previous pass rate, keyed by investigation id: the rate of the
 *  latest earlier sweep of the same agent that has one, past any that failed or
 *  stalled. Manual and custom investigations, undated ones and investigations
 *  with no predecessor have no entry. */
function previousPassRates(
  history: readonly InvestigationListItem[],
): Map<string, number> {
  const rated = newestFirst(history).filter(
    (investigation) =>
      isAmbient(investigation) &&
      startedAt(investigation) !== null &&
      passRate(investigation) !== null,
  );
  // Oldest first, so each sweep finds its agent's previous rate already noted.
  const latestRate = new Map<string, number>();
  const previous = new Map<string, number>();
  for (const investigation of rated.reverse()) {
    const agent = agentLabel(investigation);
    const before = latestRate.get(agent);
    if (before !== undefined)
      previous.set(investigation.investigation_id, before);
    // biome-ignore lint/style/noNonNullAssertion: `rated` keeps only investigations whose pass rate is not null.
    latestRate.set(agent, passRate(investigation)!);
  }
  return previous;
}

/** A rate as the whole percent a reader sees, "72%". */
function percent(rate: number): string {
  return `${Math.round(rate * 100)}%`;
}

/** How many points the pass rate moved, measured between the two whole
 *  percents shown, so the change never disagrees with them. */
function pointsMoved(rate: number, previous: number): number {
  return Math.round(rate * 100) - Math.round(previous * 100);
}

/** The share of judged trajectories that passed, as a percent, and where there
 *  is room as a bar split into passed and failed; or the investigation's state,
 *  when it has no rate, with why on hover. */
function InvestigationPassRate({
  investigation,
}: {
  investigation: InvestigationListItem;
}) {
  const rate = passRate(investigation);
  if (rate === null) {
    return (
      <span
        className={cn(
          textStyle.meta,
          resolveInvestigationOutcomeTone(investigation),
        )}
        title={stateHint(investigation)}
      >
        {formatSentenceCase(formatInvestigationOutcome(investigation))}
      </span>
    );
  }
  return (
    <span className="flex items-center justify-end gap-2">
      <span
        aria-hidden="true"
        className="hidden h-1.5 min-w-12 max-w-64 flex-1 overflow-hidden rounded-full xl:flex"
      >
        <span
          className={PASSED_SEGMENT.tone}
          style={{ width: `${rate * 100}%` }}
        />
        <span
          className={FAILED_SEGMENT.tone}
          style={{ width: `${(1 - rate) * 100}%` }}
        />
      </span>
      <span className={cn(textStyle.meta, "min-w-10 text-foreground")}>
        {formatSentenceCase(formatInvestigationOutcome(investigation))}
      </span>
    </span>
  );
}

/** How far the pass rate moved since the previous sweep, in the percent the
 *  rate is written in: "↑ 2%" in the passed colour, "↓ 4%" in red, "0%" when it
 *  held. A dash when there is no sweep before it to compare with. */
function InvestigationChange({
  rate,
  previous,
}: {
  rate: number | null;
  previous: number | undefined;
}) {
  if (rate === null || previous === undefined) {
    return <span className={textStyle.meta}>{ABSENT}</span>;
  }
  const points = pointsMoved(rate, previous);
  if (points === 0) {
    return <span className={textStyle.meta}>0%</span>;
  }
  const up = points > 0;
  return (
    // relative: holds the sr-only word, which is absolutely positioned, inside
    // the cell rather than letting it stretch the page.
    <span
      className={cn(
        textStyle.meta,
        "relative whitespace-nowrap",
        up ? TEAL : RED,
      )}
    >
      <span aria-hidden="true">{up ? "↑" : "↓"} </span>
      <span className="sr-only">{up ? "up" : "down"} </span>
      {Math.abs(points)}%
    </span>
  );
}

/** The trajectories in the investigation's window, and where there is room, for
 *  a sweep, a bar on one scale with the other sweeps', so the busiest window is
 *  the longest. A manual or custom investigation gets the count alone: its
 *  window or selection is its own. */
function TrajectoriesScanned({
  investigation,
  busiest,
}: {
  investigation: InvestigationListItem;
  busiest: number;
}) {
  const scanned = count(investigation.counters?.trajectories_scanned);
  if (scanned === 0) {
    return <span className={textStyle.meta}>{ABSENT}</span>;
  }
  return (
    <span className="flex items-center justify-end gap-2">
      <span
        aria-hidden="true"
        data-window-bar
        className="hidden h-1.5 w-16 shrink-0 xl:block"
      >
        {isAmbient(investigation) && busiest > 0 && (
          <span
            className={cn("block h-full rounded-full", WINDOW_SEGMENT.tone)}
            style={{
              width: `${Math.min(100, Math.max(4, (scanned / busiest) * 100))}%`,
            }}
          />
        )}
      </span>
      <span className={cn(textStyle.meta, "min-w-10")}>
        {scanned.toLocaleString()}
      </span>
    </span>
  );
}

/** The error, new and recurring counts, bare: their column headers say what
 *  they count. A new insight is the one to notice about the agent, so only
 *  that count is coloured; an error is the eval service's, not the agent's.
 *  A zero is muted beside the others, and a dash stands where the investigation
 *  has no counts to give. */
function InvestigationCountCells({
  investigation,
}: {
  investigation: InvestigationListItem;
}) {
  const counted = buildInvestigationStats(investigation) !== null;
  return (
    <>
      {COUNT_COLUMNS.map((col) => {
        const value = count(countersOf(investigation)[col.key]);
        return (
          <td
            key={col.key}
            className={cn(
              CELL,
              textStyle.meta,
              "text-right",
              !counted || value === 0
                ? "text-muted-foreground"
                : col.key === "insights_created"
                  ? "font-medium text-rose-600 dark:text-rose-400"
                  : "text-foreground",
            )}
          >
            {counted ? value.toLocaleString() : ABSENT}
          </td>
        );
      })}
    </>
  );
}
