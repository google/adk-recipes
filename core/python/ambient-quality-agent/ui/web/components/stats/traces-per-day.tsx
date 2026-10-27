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

import type { Day } from "@/lib/aqua-api";
import { alignToDays, DASHBOARD_DAYS } from "@/lib/day-buckets";
import { type ChartDay, DayChart, dayLabel } from "./day-chart";

/**
 * Evaluated traces per day, split by outcome.
 *
 * One column per day of the span rather than one per row the server returned.
 * `/api/daily` omits days with no sweeps, so mapping its rows straight to
 * columns silently compresses time — and the compressed chart is *more*
 * reassuring than the truth, because a fortnight with three quiet days renders
 * as an unbroken run of activity.
 *
 * Three states per column, and they are three different claims:
 *
 *   - nothing swept — hatched, no number;
 *   - swept, but every run recorded zero counters (failed, skipped or still
 *     going) — hatched too, and the tooltip says how many;
 *   - swept and measured — the stacked bar, even when the bar is zero.
 *
 * With `linkDays`, a day that ran investigations opens `/investigations?day=`,
 * which lists exactly those; a day with none stays plain, having nothing to
 * open.
 */
export function TracesPerDay({
  days,
  span = DASHBOARD_DAYS,
  now,
  height,
  linkDays = false,
}: {
  days: readonly Day[];
  span?: number;
  now?: Date;
  height?: string;
  linkDays?: boolean;
}) {
  const columns = alignToDays(days, { span, now });

  const totals = columns.reduce(
    (acc, c) => ({
      passed: acc.passed + (Number(c.row?.traces_eval_passed) || 0),
      failed: acc.failed + (Number(c.row?.traces_eval_failed) || 0),
      errored: acc.errored + (Number(c.row?.traces_eval_errored) || 0),
    }),
    { passed: 0, failed: 0, errored: 0 },
  );

  const chartDays = columns.map(({ day, row, isToday }): ChartDay => {
    const evaluated = Number(row?.traces_evaluated) || 0;
    const failed = Number(row?.traces_eval_failed) || 0;
    const unmeasured = Number(row?.unmeasured) || 0;
    return {
      day,
      isToday,
      figure: failed ? <span className="text-orange-400">{failed}</span> : null,
      // Errored and outcome-less traces count in the height but are not
      // drawn: they leave the top of the bar unfilled.
      segments: [
        { value: failed, className: "bg-orange-400" },
        {
          value: Number(row?.traces_eval_passed) || 0,
          className: "bg-teal-400",
        },
      ],
      total: evaluated,
      // A day whose every run recorded nothing is as blind as a day with no
      // runs; drawing a zero bar would claim it looked and found none.
      blind: row === null || (evaluated === 0 && unmeasured > 0),
      description: describe(day, row),
      link:
        linkDays && row && (Number(row.investigations) || 0) > 0
          ? { to: "/investigations", name: linkName(day, row) }
          : undefined,
    };
  });

  return (
    <DayChart
      days={chartDays}
      height={height}
      legend={[
        {
          swatch: "bg-teal-400",
          label: `${totals.passed.toLocaleString()} passed`,
        },
        {
          swatch: "bg-orange-400",
          label: `${totals.failed.toLocaleString()} failed`,
        },
        ...(totals.errored > 0
          ? [
              {
                label: `${totals.errored.toLocaleString()} ${
                  totals.errored === 1
                    ? "eval service error"
                    : "eval service errors"
                }`,
              },
            ]
          : []),
      ]}
      blindLabel="No trajectories evaluated"
      hint="Select a day to list its investigations."
    />
  );
}

/** One day as a sentence, for the tooltip and the a11y label. */
function describe(day: string, row: Day | null): string {
  if (row === null) return `${day}: no investigations`;
  return `${day}: ${row.traces_evaluated} evaluated, ${outcomes(row)}; ${sweeps(row)}`;
}

/**
 * A linked day's accessible name. It starts with the day as the axis prints
 * it, so it contains the visible label (WCAG 2.5.3), strided-out columns
 * included; then the investigations it opens, so the purpose is in the name
 * (WCAG 2.4.4); then the figures, and what selecting it does.
 */
function linkName(day: string, row: Day): string {
  return (
    `${dayLabel(day)}, ${day}: ${sweeps(row)}; ` +
    `${row.traces_evaluated} trajectories evaluated, ${outcomes(row)}. ` +
    "Show this day's investigations"
  );
}

function outcomes(row: Day): string {
  const errored = row.traces_eval_errored;
  return (
    `${row.traces_eval_passed} passed, ${row.traces_eval_failed} failed` +
    (errored
      ? `, ${errored} ${errored === 1 ? "eval service error" : "eval service errors"}`
      : "")
  );
}

function sweeps(row: Day): string {
  const ran = Number(row.investigations) || 0;
  const unmeasured = Number(row.unmeasured) || 0;
  return (
    (ran
      ? `${ran} investigation${ran === 1 ? "" : "s"}`
      : "no investigations") +
    (unmeasured ? `, ${unmeasured} recorded nothing` : "")
  );
}
