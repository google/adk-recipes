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

/** Issues opened and closed, per day.
 *
 * Two series and not three: New and Resolved are the two *transitions* an issue
 * makes. Recurring is not a transition — it restates every issue still open, so
 * it grows without bound and would dwarf the two numbers worth reading. The
 * chart is about what changed that day.
 *
 * Pure: no DOM, no network, and the clock only enters through `now`.
 */

import type { Day, InsightView } from "./aqua-api";
import { alignToDays, DASHBOARD_DAYS, dayKey } from "./day-buckets";

/** One column. `sweeps` is what separates a real zero from an absence. */
export interface TrendDay {
  /** `YYYY-MM-DD`, UTC. */
  day: string;
  isToday: boolean;
  created: number;
  resolved: number;
  /** The part of `resolved` dated by a recorded `resolved_at` rather than
   *  estimated; only these are on the day's insights list. */
  resolvedRecorded: number;
  /** Sweeps that closed a window on this day and measured something. Zero
   *  means nothing was measured, which is not the same as a day that measured
   *  and found nothing. */
  sweeps: number;
  /** Every sweep that closed a window on this day, measured or not, as the
   *  Activity chart counts them. */
  investigations: number;
  /** Of `investigations`, the ones that recorded nothing. */
  unmeasured: number;
}

/**
 * When an issue was resolved, or null if that cannot be said.
 *
 * Normally this just reads `resolved_at`, which the sweep that closed the issue
 * stamps. The fallback is for rows resolved before that column existed:
 * `resolve_stale_insights` deliberately never touched `updated_at` — it is the
 * last sighting, and the staleness query keys off it — so the most that can be
 * said of such a row is that some sweep closed it after its last sighting plus
 * the auto-resolve window.
 *
 * That makes the fallback an estimate, and `resolutionIsEstimated` is how the
 * chart knows to say so. With auto-resolution off there is no window to add, so
 * an unstamped row cannot be placed at all and is left out rather than guessed.
 */
export function resolutionDate(
  insight: InsightView,
  autoResolveDays: number | null,
): Date | null {
  const recorded = Date.parse(insight.resolved_at || "");
  if (Number.isFinite(recorded)) return new Date(recorded);
  if (!autoResolveDays) return null;
  const seen = Date.parse(insight.updated_at || insight.last_run_at || "");
  if (!Number.isFinite(seen)) return null;
  return new Date(seen + autoResolveDays * 86_400_000);
}

/** Whether any resolved issue on the chart had to be dated by estimate. */
export function resolutionIsEstimated(
  insights: readonly InsightView[],
): boolean {
  return insights.some(
    (i) => !Number.isFinite(Date.parse(i.resolved_at || "")),
  );
}

/**
 * Fixed day buckets over the span, filled from what the server returned.
 *
 * Fixed, not "whatever `/api/daily` sent": days with no sweeps are absent from
 * that response — deliberately, since only the caller knows which days it means
 * to show — so mapping the rows straight to columns drops the quiet days and
 * silently compresses time. A fortnight with three sweeps in it would draw as
 * three adjacent columns and read as three consecutive days.
 */
export function buildTrend({
  days,
  resolved,
  autoResolveDays,
  span = DASHBOARD_DAYS,
  now = new Date(),
}: {
  days: readonly Day[];
  resolved: readonly InsightView[];
  autoResolveDays: number | null;
  span?: number;
  now?: Date;
}): TrendDay[] {
  const columns = alignToDays(days, { span, now });
  const buckets: TrendDay[] = columns.map(({ day, row, isToday }) => {
    const totalSweeps = Number(row?.investigations) || 0;
    const unmeasured = Number(row?.unmeasured) || 0;
    const evaluated = Number(row?.traces_evaluated) || 0;
    // A day whose every run recorded nothing is as blind as a day with no
    // runs; counting unmeasured sweeps would draw a flat zero line instead of
    // the hatch that says nothing was measured.
    const sweeps =
      evaluated === 0 && unmeasured > 0
        ? 0
        : Math.max(0, totalSweeps - unmeasured);
    return {
      day,
      isToday,
      created: Number(row?.insights_created) || 0,
      resolved: 0,
      resolvedRecorded: 0,
      sweeps,
      investigations: totalSweeps,
      unmeasured,
    };
  });
  const byKey = new Map(buckets.map((b) => [b.day, b]));

  for (const insight of resolved) {
    const key = dayKey(resolutionDate(insight, autoResolveDays));
    const bucket = key ? byKey.get(key) : undefined;
    if (!bucket) continue;
    bucket.resolved += 1;
    if (!resolutionIsEstimated([insight])) bucket.resolvedRecorded += 1;
  }

  return buckets;
}

/** What the resolved series can honestly claim, or "" when it needs no note. */
export function resolvedNote(
  resolved: readonly InsightView[],
  autoResolveDays: number | null,
): string {
  if (!autoResolveDays && resolved.length === 0) {
    return "Auto-resolution is disabled, so nothing resolves.";
  }
  if (!resolutionIsEstimated(resolved)) return "";
  return (
    "Some issues were resolved before the agent recorded resolution dates; " +
    `those are placed at their last occurrence plus the ${autoResolveDays}-day ` +
    "auto-resolve window, so they are accurate to one sweep."
  );
}
