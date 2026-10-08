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

/** Fixed day columns for the per-day charts.
 *
 * `/api/daily` returns a row only for days that had a sweep — deliberately,
 * because a zero row cannot be told apart from a day the deployment was down,
 * and only the caller knows which days it means to show. Every chart drawn
 * straight from those rows therefore **compresses time**: a fortnight holding
 * three sweeps renders as three adjacent columns and reads as three
 * consecutive days.
 *
 * So the charts ask for a span and get one column per day in it, with the row
 * attached where there is one. A column with no row is a day nothing swept,
 * which every chart here draws differently from a day that swept and found
 * nothing.
 *
 * The span is also the dashboard's horizon, which is why `DASHBOARD_DAYS` and
 * `windowStart` live here rather than beside the queries: the charts' x-axis
 * and the period the totals are summed over have to be one number.
 */

/**
 * How far back the dashboard looks, in days.
 *
 * There is no period control, and this is the whole of what one would have
 * been. v1 offered 7 and 30 and had to, because its charts drew whatever the
 * response held and the select was the only statement of how much time was on
 * screen; v2's charts span a fixed number of days on their own, so a control
 * would be a second way to set an axis that already has one.
 *
 * Fourteen because that is `insights_auto_resolve_days`' default: an issue
 * unseen for that long is retired, so a fortnight is the span over which
 * everything still considered live was seen. The two are not wired together —
 * an operator lengthening the resolve window should not silently rescale every
 * chart — but the number was chosen from it.
 *
 * Making this a variable is the whole of the deferred period control: it is
 * read by both charts and by the two scoped queries, so one module changes.
 */
export const DASHBOARD_DAYS = 14;

/** How to name the horizon in a caption, so no view can state a different one. */
export const HORIZON_LABEL = `last ${DASHBOARD_DAYS} days`;

/** Midnight UTC on the first day of a span ending today. */
function spanStart(span: number, now: Date): Date {
  const start = new Date(now);
  start.setUTCHours(0, 0, 0, 0);
  start.setUTCDate(start.getUTCDate() - (span - 1));
  return start;
}

/**
 * The `windowStart` the scoped reads send, as an ISO instant.
 *
 * Day-aligned, and that is the point: the server filters on an instant, so
 * `now - 14 days` would open the window mid-morning and the oldest column
 * would hold only the sweeps after that hour — a bar systematically short by
 * however far into the day the reader happens to be looking. It shares
 * `spanStart` with `alignToDays` so the window cannot drift from the leftmost
 * column it fills.
 */
export function windowStart(span: number, now: Date = new Date()): string {
  return spanStart(span, now).toISOString();
}

/** UTC day key.
 *
 * UTC rather than local, to agree with the server: `/api/daily` buckets on
 * `DATE(window_end)`, a UTC date. Bucketing locally would file a 23:30 UTC
 * sweep under tomorrow for a reader east of Greenwich while the totals counted
 * it today — an off-by-one that changes with who is looking.
 */
export function dayKey(value: Date | string | null | undefined): string | null {
  if (value === null || value === undefined || value === "") return null;
  const d = value instanceof Date ? value : new Date(value);
  if (Number.isNaN(d.getTime())) return null;
  const month = String(d.getUTCMonth() + 1).padStart(2, "0");
  const day = String(d.getUTCDate()).padStart(2, "0");
  return `${d.getUTCFullYear()}-${month}-${day}`;
}

/**
 * A `?day=` search param as a UTC day key, or undefined if it names no real day.
 *
 * Numbers are read as their digits because the router hands `?day=20260920`
 * over as one; it is still not a day. Anything dropped here reads as no day at
 * all, rather than as a filter that matches nothing under a chip naming a date
 * the calendar does not have.
 */
export function parseDay(value: unknown): string | undefined {
  if (typeof value !== "string" && typeof value !== "number") return undefined;
  const text = String(value);
  if (!/^\d{4}-\d{2}-\d{2}$/.test(text)) return undefined;
  // Round-tripped, so February 30th, which `Date` rolls into March, is refused.
  return dayKey(`${text}T00:00:00Z`) === text ? text : undefined;
}

/**
 * The `windowStart`/`windowEnd` pair covering one UTC day, both inclusive.
 *
 * The end is the day's last microsecond, BigQuery's resolution, rather than the
 * next midnight: the server's bound is inclusive, and a run whose window
 * closed on the stroke of midnight is the next day's column.
 */
export function dayWindow(day: string): {
  windowStart: string;
  windowEnd: string;
} {
  return {
    windowStart: `${day}T00:00:00.000Z`,
    windowEnd: `${day}T23:59:59.999999Z`,
  };
}

/** One column: the day, and whatever the server said about it. */
export interface DayColumn<T> {
  /** `YYYY-MM-DD`, UTC. */
  day: string;
  /** The server's row, or null when nothing swept that day. */
  row: T | null;
  /** Today's column, for the highlight that tells a reader where "now" is —
   *  the rightmost column is only today if the span ends today. */
  isToday: boolean;
}

/**
 * One column per day of the span, ending today, oldest first.
 *
 * A row is matched by its `day` key, so a row outside the span is dropped
 * rather than clamped into the nearest column: it belongs to a day this chart
 * is not showing, and folding it in would overstate that day.
 */
export function alignToDays<T extends { day: string }>(
  rows: readonly T[],
  { span, now = new Date() }: { span: number; now?: Date },
): Array<DayColumn<T>> {
  const byDay = new Map(rows.map((r) => [r.day, r]));
  const today = dayKey(now);
  const start = spanStart(span, now);

  const columns: Array<DayColumn<T>> = [];
  for (let i = 0; i < span; i++) {
    const date = new Date(start);
    date.setUTCDate(start.getUTCDate() + i);
    const day = dayKey(date) as string;
    columns.push({ day, row: byDay.get(day) ?? null, isToday: day === today });
  }
  return columns;
}

/**
 * Which columns get a label, so they stay readable as the span grows.
 *
 * Above ten columns the labels collide, and the fix is not a smaller font: it
 * is fewer of them. Every nth is labelled, counting back from the right so the
 * most recent day is always one of them — that is the column a reader looks
 * for first.
 */
export function labelStride(count: number, max = 10): number {
  return count <= max ? 1 : Math.ceil(count / max);
}

export function isLabelled(index: number, count: number, max = 10): boolean {
  return (count - 1 - index) % labelStride(count, max) === 0;
}
