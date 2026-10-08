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

/** How a run describes itself on a row and in its detail header.
 *
 * Pure functions over a run record: no DOM, no clock beyond what is passed in,
 * no network. The runs table and the run detail render the same facts in
 * different shapes, and a sweep that reported one telemetry window on the list
 * and another on its own page would be a dashboard disagreeing with itself.
 */

import type { InvestigationCounters, Stats } from "./aqua-api";
import { isNoData } from "./failure";
import { count, evaluatedTraces } from "./funnel";
import { isStalled, startedAt, type RunLike } from "./runstate";

/** What these helpers need off a run. A structural subset of `Run` so the
 *  helpers stay testable against a literal, and so a caller holding a partial
 *  record -- an older row missing half its columns -- still type-checks. */
export interface RunSummaryLike extends RunLike {
  status?: string | null;
  error?: string | null;
  observed_agent_name?: string | null;
  trigger_type?: string | null;
  window_start?: string | null;
  window_end?: string | null;
  due_at?: string | null;
  counters?: InvestigationCounters;
  metrics_passed?: number | null;
  metrics_failed?: number | null;
  metrics_errored?: number | null;
}

/** The em dash every unset field renders as, so "nothing recorded" looks the
 *  same everywhere rather than alternating with a blank cell. */
export const ABSENT = "—";

/** One instant, as the old dashboard wrote it: local time, short month, no year
 *  and no seconds, "Sep 24, 8:00 AM". A value that will not parse is passed
 *  through verbatim rather than rendered as absent -- a malformed timestamp is
 *  a fact about the record, and hiding it behind an em dash loses it. */
export function formatInstant(iso: string | null | undefined): string {
  if (!iso) return ABSENT;
  const d = new Date(iso);
  return Number.isNaN(d.getTime())
    ? iso
    : d.toLocaleString(undefined, {
        month: "short",
        day: "numeric",
        hour: "numeric",
        minute: "2-digit",
      });
}

/** The telemetry window a sweep covered, as `start – end`.
 *
 * This is the period the run looked at, not how long it took -- the two are
 * unrelated, and the list column that used to show neither is the one a reader
 * uses to tell two sweeps of the same agent apart. Absent only when *both*
 * bounds are missing: a half-open window still says which half it knows.
 */
export function telemetryWindow(run: RunSummaryLike): string {
  const start = formatInstant(run.window_start);
  const end = formatInstant(run.window_end);
  if (start === ABSENT && end === ABSENT) return ABSENT;
  return `${start} – ${end}`;
}

/** How far from its investigation's start a window may end and still count as
 *  ending there. A derived window ends when its record is created
 *  (`derive_window_and_budget` in job_scheduling.py), so this only has to
 *  absorb a slow write. */
const WINDOW_END_SLACK_MS = 5 * 60 * 1000;

/** The length of a telemetry window that ends when its investigation started,
 * coarsely: "45m", "6h", "7d". Every derived window ends there, so with the
 * start known the length says which period the run covered.
 *
 * Null for a window that ends anywhere else, which only its bounds describe
 * (see `telemetryWindow`), and for one missing a bound.
 */
export function windowLength(run: RunSummaryLike): string | null {
  const from = Date.parse(run.window_start ?? "");
  const to = Date.parse(run.window_end ?? "");
  const started = startedAt(run);
  if (
    started === null ||
    !(to > from) ||
    Math.abs(started - to) > WINDOW_END_SLACK_MS
  ) {
    return null;
  }
  const minutes = Math.max(1, Math.round((to - from) / 60000));
  const hours = Math.round(minutes / 60);
  return minutes < 60
    ? `${minutes}m`
    : hours < 48
      ? `${hours}h`
      : `${Math.round(hours / 24)}d`;
}

/** A run's counters in the shape of the totals, so the funnel can draw one run
 *  as it draws them all. Null when every counter is zero: a sweep still
 *  running, or a record older than the counters, neither of which measured
 *  anything. */
export function runStats(run: RunSummaryLike): Stats | null {
  const counters = run.counters;
  if (!counters || !Object.values(counters).some((v) => count(v) > 0))
    return null;
  return { investigations: 1, ...counters };
}

/** Why a run has no counts to draw, for where its funnel would be. A run still
 *  going writes its counters when it finishes, and a failed one writes none
 *  (`run_investigation_graph` in core/investigation/job_execution.py). */
export function noCountsText(run: RunSummaryLike): string {
  const status = (run.status || "").toLowerCase();
  if (run.error || status === "failed")
    return "No counts: a failed investigation records none.";
  if (status === "scheduled") {
    return "No counts yet: the investigation has not started.";
  }
  if (status === "running" || status === "pending") {
    return "No counts yet: they are recorded when the investigation finishes.";
  }
  return "No counts recorded.";
}

/** Whether a run is waiting for the delayed trigger an agent redeploy set. */
export function isScheduled(run: RunSummaryLike): boolean {
  return (run.status || "").toLowerCase() === "scheduled";
}

/** Whether a scheduled run's trigger is due and has not started it. The
 *  server fails such a run at the first submission after
 *  `SCHEDULED_RUN_GRACE_MINUTES` (core/investigation/job_scheduling.py); until
 *  then the row says it is late rather than still waiting. */
export function isPastDue(
  run: RunSummaryLike,
  now: number = Date.now(),
): boolean {
  if (!isScheduled(run)) return false;
  const due = Date.parse(run.due_at || "");
  return Number.isFinite(due) && now > due;
}

/** When a scheduled run starts, and why it waits, for a tooltip or a header.
 *  Null for a run that is not scheduled. */
export function renderScheduledText(
  run: RunSummaryLike,
  now: number = Date.now(),
): string | null {
  if (!isScheduled(run)) return null;
  const due = formatInstant(run.due_at);
  if (isPastDue(run, now)) {
    return `Was due to start at ${due} and has not started yet.`;
  }
  return (
    `Starts at ${due}. After the agent is redeployed, AQuA waits so that ` +
    `the new revision's traces can accumulate.`
  );
}

/** What to call the agent a sweep observed. Named rather than left blank: a row
 *  with no agent is a record worth noticing, not an empty cell to skip over. */
export function agentLabel(run: RunSummaryLike): string {
  return run.observed_agent_name || "(unnamed agent)";
}

/** True only when the run can be *shown* to have finished with nothing failing.
 *
 * Read off the per-trace counters when it has them, falling back to the
 * `(case, metric)` tallies for a run recorded before they existed. Neither
 * available means "cannot confirm", which is false here -- requiring positive
 * evidence of at least one evaluated pass before calling a sweep clean. For the
 * runs table's "Only with failures" filter, see `hasFailures`.
 */
export function metricsAllPassed(run: RunSummaryLike): boolean {
  if ((run.status || "").toLowerCase() !== "done" || run.error) return false;
  const c = run.counters;
  if (c && Number(c.traces_evaluated) > 0) {
    return (
      Number(c.traces_eval_passed) > 0 &&
      Number(c.traces_eval_failed) === 0 &&
      Number(c.traces_eval_errored) === 0
    );
  }
  return (
    Number(run.metrics_passed) > 0 &&
    Number(run.metrics_failed) === 0 &&
    Number(run.metrics_errored) === 0
  );
}

/** Names a run's state when it has no evaluated traces to count: "skipped",
 *  "failed", "no data" (a sweep that failed because there was nothing to
 *  read), "scheduled", "stalled", "running", "pending" or "no trajectories
 *  evaluated". Returns null for a run with evaluated traces, whose pass and
 *  fail counts say more. The runs table and the rail's Recent investigations
 *  both word runs with this. */
export function runStateWord(run: RunSummaryLike): string | null {
  const status = (run.status || "").toLowerCase();
  if (status === "skipped") return "skipped";
  if (run.error) return isNoData(run.error) ? "no data" : "failed";
  if (status === "failed") return "failed";
  if (status === "scheduled") return "scheduled";
  if (isStalled(run)) return "stalled";
  if (status === "running" || status === "pending") return status;
  if (evaluatedTraces(run.counters ?? {}) === 0)
    return "no trajectories evaluated";
  return null;
}

/** The share of a run's judged trajectories that passed: passed over passed
 *  and failed. Eval-service errors are left out, being a failure of the eval
 *  service rather than of the agent (`traces_eval_errored` in
 *  tools/investigations/models.py). Null for a run whose row names a state
 *  instead, and for one whose every evaluation errored. */
export function passRate(run: RunSummaryLike): number | null {
  if (runStateWord(run)) return null;
  const passed = count(run.counters?.traces_eval_passed);
  const judged = passed + count(run.counters?.traces_eval_failed);
  return judged > 0 ? passed / judged : null;
}

/** Whether a run is an ambient sweep. Sweeps each cover the time since the
 *  last one, so their figures compare; a manual or custom run chooses its own
 *  window or conversations (`NON_AMBIENT_TRIGGER_TYPES` in
 *  tools/investigations/models.py). A record with no trigger predates the
 *  field and is taken for a sweep, the common case. */
export function isAmbient(run: RunSummaryLike): boolean {
  const trigger = (run.trigger_type || "").toLowerCase();
  return trigger !== "manual" && trigger !== "custom";
}

const FAILURE_COUNTER_KEYS: readonly (keyof InvestigationCounters)[] = [
  "traces_ingested_failed",
  "traces_eval_failed",
  "traces_eval_errored",
  "rubrics_errored",
  "rubrics_unclustered",
  "clusters_verify_failed",
  "findings_generated",
  "clusters_verified",
  "insights_created",
  "insights_recurring",
];

/** True when the run itself failed, stalled, or recorded any failing trace, metric, or finding.
 *
 * Used by the "Only with failures" filter on the investigations table so idle
 * `done` sweeps that scanned zero traces (`— / — / —`), duplicate-trigger
 * `skipped` runs, and empty-deployment `no-data` rows are filtered out along
 * with clean passes.
 */
export function hasFailures(run: RunSummaryLike): boolean {
  const status = (run.status || "").toLowerCase();
  if (status === "skipped") return false;
  if (run.error) return !isNoData(run.error);
  if (status === "failed" || isStalled(run)) return true;
  const c = run.counters;
  if (c) {
    if (FAILURE_COUNTER_KEYS.some((k) => count(c[k]) > 0)) return true;
    if (count(c.traces_scanned) > 0 && count(c.traces_evaluated) === 0) {
      return true;
    }
  }
  return (
    count(run.metrics_failed ?? undefined) > 0 ||
    count(run.metrics_errored ?? undefined) > 0
  );
}
