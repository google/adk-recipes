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

/** How an investigation describes itself on a row and in its detail header.
 *
 * Pure functions over an investigation record: no DOM, no clock beyond what is
 * passed in, no network. The investigations table and the investigation detail
 * render the same facts in different shapes, and a sweep that reported one
 * telemetry window on the list and another on its own page would be a dashboard
 * disagreeing with itself.
 */

import type { InvestigationCounters, Stats } from "./aqua-api";
import { isNoData } from "./failure";
import { count, countEvaluatedTrajectories } from "./funnel";
import {
  isStalled,
  startedAt,
  type InvestigationLike,
} from "./investigation-state";

/** What these helpers need off an investigation. A structural subset of
 *  `InvestigationListItem` so the helpers stay testable against a literal, and
 *  so a caller holding a partial record -- an older row missing half its
 *  columns -- still type-checks. */
export interface InvestigationRecordLike extends InvestigationLike {
  status?: string | null;
  error?: string | null;
  observed_agent_key?: string | null;
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

/** Formats an ISO timestamp in local time with short month, day, hour, and
 *  minute ("Sep 24, 8:00 AM"), without year or seconds. Unparseable strings are
 *  returned verbatim rather than replaced with `ABSENT` so malformed values
 *  remain visible. */
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
 * This is the period the investigation looked at, not how long it took, and is
 * what a reader uses to tell two sweeps of the same agent apart. Absent only
 * when *both* bounds are missing: a half-open window still says which half it
 * knows.
 */
export function telemetryWindow(
  investigation: InvestigationRecordLike,
): string {
  const start = formatInstant(investigation.window_start);
  const end = formatInstant(investigation.window_end);
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
 * start known the length says which period the investigation covered.
 *
 * Null for a window that ends anywhere else, which only its bounds describe
 * (see `telemetryWindow`), and for one missing a bound.
 */
export function windowLength(
  investigation: InvestigationRecordLike,
): string | null {
  const from = Date.parse(investigation.window_start ?? "");
  const to = Date.parse(investigation.window_end ?? "");
  const started = startedAt(investigation);
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

/** An investigation's counters in the shape of the totals, so the funnel can
 *  draw one investigation as it draws them all. Null when every counter is
 *  zero: a sweep still running, or a record older than the counters, neither
 *  of which measured anything. */
export function buildInvestigationStats(
  investigation: InvestigationRecordLike,
): Stats | null {
  const counters = investigation.counters;
  if (!counters || !Object.values(counters).some((v) => count(v) > 0))
    return null;
  return { investigations: 1, ...counters };
}

/** Why an investigation has no counts to draw, for where its funnel would be.
 *  An investigation still going writes its counters when it finishes, and a
 *  failed one writes none (`run_investigation_graph` in
 *  core/investigation/job_execution.py). */
export function noCountsText(investigation: InvestigationRecordLike): string {
  const status = (investigation.status || "").toLowerCase();
  if (investigation.error || status === "failed")
    return "No counts: a failed investigation records none.";
  if (status === "scheduled") {
    return "No counts yet: the investigation has not started.";
  }
  if (status === "running" || status === "pending") {
    return "No counts yet: they are recorded when the investigation finishes.";
  }
  return "No counts recorded.";
}

/** Whether an investigation is waiting for the delayed trigger an agent
 *  redeploy set. */
export function isScheduled(investigation: InvestigationRecordLike): boolean {
  return (investigation.status || "").toLowerCase() === "scheduled";
}

/** Whether a scheduled investigation's trigger is due and has not started it.
 *  The server fails such an investigation at the first submission after
 *  `SCHEDULED_INVESTIGATION_GRACE_MINUTES` (core/investigation/job_scheduling.py); until
 *  then the row says it is late rather than still waiting. */
export function isPastDue(
  investigation: InvestigationRecordLike,
  now: number = Date.now(),
): boolean {
  if (!isScheduled(investigation)) return false;
  const due = Date.parse(investigation.due_at || "");
  return Number.isFinite(due) && now > due;
}

/** When a scheduled investigation starts, and why it waits, for a tooltip or a
 *  header. Null for an investigation that is not scheduled. */
export function renderScheduledText(
  investigation: InvestigationRecordLike,
  now: number = Date.now(),
): string | null {
  if (!isScheduled(investigation)) return null;
  const due = formatInstant(investigation.due_at);
  if (isPastDue(investigation, now)) {
    return `Was due to start at ${due} and has not started yet.`;
  }
  return (
    `Starts at ${due}. After the agent is redeployed, AQuA waits so that ` +
    `the new revision's traces can accumulate.`
  );
}

/** What to call the agent a sweep observed. Named rather than left blank: a row
 *  with no agent is a record worth noticing, not an empty cell to skip over. */
export function agentLabel(investigation: InvestigationRecordLike): string {
  return investigation.observed_agent_key || "(unnamed agent)";
}

/** True only when the investigation can be *shown* to have finished with
 * nothing failing.
 *
 * Reads the trajectory counters when present, falling back to the
 * `(trajectory, metric)` tallies when trajectory counters are zero. Neither
 * available means "cannot confirm", which is false here -- requiring positive
 * evidence of at least one evaluated pass before calling a sweep clean. For the
 * investigations table's "Only with failures" filter, see `hasFailures`.
 */
export function metricsAllPassed(
  investigation: InvestigationRecordLike,
): boolean {
  if (
    (investigation.status || "").toLowerCase() !== "done" ||
    investigation.error
  )
    return false;
  const c = investigation.counters;
  if (c && Number(c.trajectories_evaluated) > 0) {
    return (
      Number(c.trajectories_eval_passed) > 0 &&
      Number(c.trajectories_eval_failed) === 0 &&
      Number(c.trajectories_eval_errored) === 0
    );
  }
  return (
    Number(investigation.metrics_passed) > 0 &&
    Number(investigation.metrics_failed) === 0 &&
    Number(investigation.metrics_errored) === 0
  );
}

/** Names an investigation's state when it has no evaluated trajectories to count:
 *  "skipped", "failed", "no data" (a sweep that failed because there was
 *  nothing to read), "scheduled", "stalled", "running", "pending" or "no
 *  trajectories evaluated". Returns null for an investigation with evaluated
 *  trajectories, whose pass and fail counts say more. The investigations table
 *  and the rail's Recent investigations both word investigations with this. */
export function formatInvestigationStateWord(
  investigation: InvestigationRecordLike,
): string | null {
  const status = (investigation.status || "").toLowerCase();
  if (status === "skipped") return "skipped";
  if (investigation.error)
    return isNoData(investigation.error) ? "no data" : "failed";
  if (status === "failed") return "failed";
  if (status === "scheduled") return "scheduled";
  if (isStalled(investigation)) return "stalled";
  if (status === "running" || status === "pending") return status;
  if (countEvaluatedTrajectories(investigation.counters ?? {}) === 0)
    return "no trajectories evaluated";
  return null;
}

/** The share of an investigation's judged trajectories that passed: passed over
 *  passed and failed. Eval-service errors are left out, being a failure of the
 *  eval service rather than of the agent (`trajectories_eval_errored` in
 *  tools/investigations/models.py). Null for an investigation whose row names a
 *  state instead, and for one whose every evaluation errored. */
export function passRate(
  investigation: InvestigationRecordLike,
): number | null {
  if (formatInvestigationStateWord(investigation)) return null;
  const passed = count(investigation.counters?.trajectories_eval_passed);
  const judged =
    passed + count(investigation.counters?.trajectories_eval_failed);
  return judged > 0 ? passed / judged : null;
}

/** Whether an investigation is an ambient sweep. Sweeps each cover the time
 *  since the last one, so their figures compare; a manual or custom
 *  investigation chooses its own window or conversations
 *  (`NON_AMBIENT_TRIGGER_TYPES` in tools/investigations/models.py). A record
 *  with no trigger is treated as an ambient sweep. */
export function isAmbient(investigation: InvestigationRecordLike): boolean {
  const trigger = (investigation.trigger_type || "").toLowerCase();
  return trigger !== "manual" && trigger !== "custom";
}

/** Who started an investigation: `custom` for a one-off investigation of
 *  conversations a user picked (usually from chat), `manual` for a sweep a user
 *  or the CLI asked for, and `ambient` for every trigger the loop fires on its
 *  own (`scheduled`, and `task_fire` after an observed-agent redeploy). */
export type TriggerKind = "custom" | "manual" | "ambient";

/** Sorts an investigation into its `TriggerKind`, case-insensitively.
 *
 * Args:
 *   investigation: The investigation record, read for its `trigger_type`.
 *
 * Returns:
 *   `custom` or `manual` for a user-initiated investigation; `ambient` for
 *   anything else, including a record with an empty `trigger_type`.
 */
export function resolveTriggerKind(
  investigation: InvestigationRecordLike,
): TriggerKind {
  const trigger = (investigation.trigger_type || "").toLowerCase();
  if (trigger === "custom" || trigger === "manual") return trigger;
  return "ambient";
}

/** Pulls the reviewer focus a custom investigation was started with off its
 * overrides.
 *
 * Args:
 *   investigation: The investigation record; `custom_overrides` is null for an
 *     ambient investigation.
 *
 * Returns:
 *   The trimmed focus, or null when the investigation has none.
 */
export function extractReviewFocus(investigation: {
  custom_overrides?: { review_focus?: string | null } | null;
}): string | null {
  return investigation.custom_overrides?.review_focus?.trim() || null;
}

const FAILURE_COUNTER_KEYS: readonly (keyof InvestigationCounters)[] = [
  "trajectories_ingested_failed",
  "trajectories_eval_failed",
  "trajectories_eval_errored",
  "findings_errored",
  "findings_unclustered",
  "clusters_verify_failed",
  "findings_generated",
  "clusters_verified",
  "insights_created",
  "insights_recurring",
];

/** True when the investigation itself failed, stalled, or recorded any failing
 * trajectory, metric, or finding.
 *
 * Used by the "Only with failures" filter on the investigations table so idle
 * `done` sweeps that scanned zero trajectories (`— / — / —`), duplicate-trigger
 * `skipped` investigations, and empty-deployment `no-data` rows are filtered
 * out along with clean passes.
 */
export function hasFailures(investigation: InvestigationRecordLike): boolean {
  const status = (investigation.status || "").toLowerCase();
  if (status === "skipped") return false;
  if (investigation.error) return !isNoData(investigation.error);
  if (status === "failed" || isStalled(investigation)) return true;
  const c = investigation.counters;
  if (c) {
    if (FAILURE_COUNTER_KEYS.some((k) => count(c[k]) > 0)) return true;
    if (
      count(c.trajectories_scanned) > 0 &&
      count(c.trajectories_evaluated) === 0
    ) {
      return true;
    }
  }
  return (
    count(investigation.metrics_failed ?? undefined) > 0 ||
    count(investigation.metrics_errored ?? undefined) > 0
  );
}
