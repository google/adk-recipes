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
import type { Run } from "@/lib/aqua-api";
import { describeFailure, isNoData } from "@/lib/failure";
import { count, STAGES, type StageId } from "@/lib/funnel";
import {
  ABSENT,
  agentLabel,
  formatInstant,
  isAmbient,
  isPastDue,
  passRate,
  runStateWord,
  runStats,
  renderScheduledText,
  telemetryWindow,
  windowLength,
} from "@/lib/run-summary";
import { inFlightAge, isStalled, newestFirst, startedAt } from "@/lib/runstate";
import { link, mono, textStyle } from "@/lib/typography";
import { cn, formatSentenceCase } from "@/lib/utils";

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

const WINDOW_SEGMENT = segment("window", "traces_scanned");
const PASSED_SEGMENT = segment("evaluated", "traces_eval_passed");
const FAILED_SEGMENT = segment("evaluated", "traces_eval_failed");

/** The counts a row gives bare, one right-aligned column each, in the funnel's
 *  colours: eval-service errors, then new and recurring insights. Named rather
 *  than taken whole from the funnel's stages, which can gain segments that no
 *  single run counts. */
const COUNT_COLUMNS = [
  { ...segment("evaluated", "traces_eval_errored"), label: "errors" },
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
 * out: the agent, which the tab bar also names, unless the runs observed more
 * than one; and the window, unless this run's differs from the one most of the
 * others share. The start time's tooltip gives the window either way.
 *
 * Only ambient sweeps are compared: the change in pass rate and the length of
 * the traffic bar are measured against other sweeps, never against a manual or
 * custom run that chose its own window or conversations.
 *
 * Home's recent investigations and the investigations page both draw their
 * runs with this, so the two read alike.
 */
export function RunsTable({
  runs,
  history = runs,
}: {
  runs: readonly Run[];
  /** The whole list the rows were cut or filtered from. Each row's previous
   *  investigation is looked for in it, so the last row of five, or the row
   *  after a hidden one, still has one to compare with; and the traffic bars
   *  are scaled to its busiest sweep, so a row's bar keeps its length when the
   *  rows are filtered. */
  history?: readonly Run[];
}) {
  const navigate = useNavigate();
  const router = useRouter();
  const showAgent = new Set(runs.map(agentLabel)).size > 1;
  const usualWindow = mostCommon(runs.map(windowNote));
  const busiest = Math.max(
    0,
    ...history
      .filter(isAmbient)
      .map((run) => count(run.counters?.traces_scanned)),
  );
  const previousRates = previousPassRates(history);
  return (
    // A phone still scrolls it sideways: scroll-x-edges shades the side that
    // has more, so the columns past the edge are not a secret.
    <div className="scroll-x-edges overflow-x-auto rounded-lg border">
      <table className={cn(textStyle.body, "w-full text-left")}>
        <thead className={textStyle.label}>
          <tr className="border-b">
            <th className={cn(CELL, "font-medium")}>Started</th>
            {showAgent && <th className={cn(CELL, "font-medium")}>Agent</th>}
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
              <th key={col.key} className={cn(CELL, "text-right font-medium")}>
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
          {runs.map((run) => {
            const note = windowNote(run);
            const bounds = telemetryWindow(run);
            const href = router.buildLocation({
              to: "/investigations/$runId",
              params: { runId: run.run_id },
            }).href;
            return (
              <tr
                key={run.run_id}
                // The run id is the row's link, for the keyboard and a
                // screen reader. A click elsewhere on the row goes to the
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
                    to: "/investigations/$runId",
                    params: { runId: run.run_id },
                  });
                }}
                onMouseDown={(e) => {
                  if (e.button === 1 && !(e.target as Element).closest("a")) {
                    e.preventDefault();
                  }
                }}
                onAuxClick={(e) => {
                  if (e.button === 1 && !(e.target as Element).closest("a")) {
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
                      title={bounds === ABSENT ? undefined : `Window ${bounds}`}
                    >
                      {startedLabel(run)}
                    </span>
                    <Link
                      to="/investigations/$runId"
                      params={{ runId: run.run_id }}
                      className={cn(textStyle.meta, mono, link.standalone)}
                    >
                      {run.run_id.slice(0, 8)}
                    </Link>
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
                </td>
                {showAgent && (
                  <td className={cn(CELL, textStyle.meta)}>
                    {agentLabel(run)}
                  </td>
                )}
                <td className={cn(CELL, "hidden text-right lg:table-cell")}>
                  <RunTrajectories run={run} busiest={busiest} />
                </td>
                <td className={cn(CELL, "text-right")}>
                  <RunPassRate run={run} />
                </td>
                <td className={cn(CELL, "text-right")}>
                  <RunChange
                    rate={passRate(run)}
                    previous={previousRates.get(run.run_id)}
                  />
                </td>
                <RunCountCells run={run} />
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
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
 *  window", or its bounds when it did not end where the run started. */
function windowNote(run: Run): string {
  const length = windowLength(run);
  return length ? `${length} window` : telemetryWindow(run);
}

/** "Sep 24, 8:00 AM", by whichever start time the record carries. */
function startedLabel(run: Run): string {
  const t = startedAt(run);
  return t === null ? "Undated" : formatInstant(new Date(t).toISOString());
}

/** A run's outcome: its pass rate, "74%" of the trajectories judged
 *  (eval-service errors are not a judgement), or its failure/in-flight status.
 *  A dash when every evaluation errored. */
export function runOutcome(run: Run): string {
  const state = runStateWord(run);
  if (state) return state;
  const rate = passRate(run);
  return rate === null ? ABSENT : percent(rate);
}

/** The colour `runOutcome` is written in: red for a genuine failure, amber for
 *  a stalled run or a scheduled one past its due time, muted for the rest. A
 *  sweep that failed for want of telemetry is muted too: a column of red on a
 *  deployment nobody has used yet is the wrong thing to tell whoever just set
 *  it up. */
export function runOutcomeTone(run: Run): string {
  const status = (run.status || "").toLowerCase();
  if (status === "skipped") return "text-muted-foreground";
  if (run.error) {
    return isNoData(run.error) ? "text-muted-foreground" : RED;
  }
  if (status === "failed") return RED;
  if (isStalled(run) || isPastDue(run)) {
    return "text-amber-700 dark:text-amber-400";
  }
  return "text-muted-foreground";
}

/** Why a run is in the state its row names: the error it failed with, what a
 *  failure for want of telemetry means, when a scheduled run starts, or how
 *  long a stalled run has gone without an update. Undefined when the state
 *  explains itself. */
function stateHint(run: Run): string | undefined {
  if (run.error) {
    const failure = describeFailure(run.error);
    return failure.kind === "no-data"
      ? (failure.hint ?? failure.cause)
      : run.error;
  }
  const scheduled = renderScheduledText(run);
  if (scheduled) return scheduled;
  if (!isStalled(run)) return undefined;
  // The row says "stalled" while the record says running, and Run
  // investigation is available again: the reader is owed the reason.
  const status = (run.status || "in flight").toLowerCase();
  return (
    `No update for ${inFlightAge(run)}. The record still says ${status}, ` +
    `but a new investigation can be started.`
  );
}

/** A run's counters, keyed as the funnel's segments are: they are named after
 *  the totals, which sum these same counters. */
function countersOf(run: Run): Record<string, number | undefined> {
  return (run.counters ?? {}) as Record<string, number | undefined>;
}

/** Each sweep's previous pass rate, keyed by run id: the rate of the latest
 *  earlier sweep of the same agent that has one, past any that failed or
 *  stalled. Manual and custom runs, undated ones and runs with no predecessor
 *  have no entry. */
function previousPassRates(history: readonly Run[]): Map<string, number> {
  const rated = newestFirst(history).filter(
    (run) =>
      isAmbient(run) && startedAt(run) !== null && passRate(run) !== null,
  );
  // Oldest first, so each sweep finds its agent's previous rate already noted.
  const latestRate = new Map<string, number>();
  const previous = new Map<string, number>();
  for (const run of rated.reverse()) {
    const agent = agentLabel(run);
    const before = latestRate.get(agent);
    if (before !== undefined) previous.set(run.run_id, before);
    // biome-ignore lint/style/noNonNullAssertion: `rated` keeps only runs whose pass rate is not null.
    latestRate.set(agent, passRate(run)!);
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
 *  is room as a bar split into passed and failed; or the run's state, when it
 *  has no rate, with why on hover. */
function RunPassRate({ run }: { run: Run }) {
  const rate = passRate(run);
  if (rate === null) {
    return (
      <span
        className={cn(textStyle.meta, runOutcomeTone(run))}
        title={stateHint(run)}
      >
        {formatSentenceCase(runOutcome(run))}
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
        {formatSentenceCase(runOutcome(run))}
      </span>
    </span>
  );
}

/** How far the pass rate moved since the previous sweep, in the percent the
 *  rate is written in: "↑ 2%" in the passed colour, "↓ 4%" in red, "0%" when it
 *  held. A dash when there is no sweep before it to compare with. */
function RunChange({
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

/** The trajectories in the run's window, and where there is room, for a sweep,
 *  a bar on one scale with the other sweeps', so the busiest window is the
 *  longest. A manual or custom run gets the count alone: its window or
 *  selection is its own. */
function RunTrajectories({ run, busiest }: { run: Run; busiest: number }) {
  const scanned = count(run.counters?.traces_scanned);
  if (scanned === 0) {
    return <span className={textStyle.meta}>{ABSENT}</span>;
  }
  return (
    <span className="flex items-center justify-end gap-2">
      <span aria-hidden="true" className="hidden h-1.5 w-16 shrink-0 xl:block">
        {isAmbient(run) && busiest > 0 && (
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
 *  A zero is muted beside the others, and a dash stands where the run has no
 *  counts to give. */
function RunCountCells({ run }: { run: Run }) {
  const counted = runStats(run) !== null;
  return (
    <>
      {COUNT_COLUMNS.map((col) => {
        const value = count(countersOf(run)[col.key]);
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
