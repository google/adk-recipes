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

import type { TrendDay } from "@/lib/insight-trend";
import { type ChartDay, DayChart, dayLabel } from "./day-chart";

/**
 * Issues created and resolved per day.
 *
 * The funnel above this chart is a total, and the findings table beside it is
 * a snapshot of what is open right now. Neither says whether the count is
 * growing or shrinking, which meant the one longitudinal question the quality
 * pipeline exists to answer — is this agent getting better or worse? — had no
 * picture. New stacks on Resolved, both single digits a day, so the column is
 * the day's churn and its height is how much moved.
 *
 * A day nothing swept is hatched rather than drawn as a zero. The two are
 * different claims and the difference is the whole point of the chart: a flat
 * week because the agent is clean and a flat week because the loop stopped
 * turning look identical otherwise.
 *
 * A day that found something, or recorded a resolution, opens
 * `/insights?day=`, which lists those insights. A day with neither stays plain,
 * and so does one whose only resolutions are estimated: the list files a
 * resolution by its recorded `resolved_at`, so it would open empty.
 */
export function InsightTrend({
  days,
  note,
  height,
}: {
  days: TrendDay[];
  note?: string;
  height?: string;
}) {
  const chartDays = days.map((d): ChartDay => {
    const total = d.created + d.resolved;
    return {
      day: d.day,
      isToday: d.isToday,
      // New, and resolved after a plus: the two the day's list holds. The plus
      // is neutral, so each colour marks only its own count.
      figure:
        total === 0 ? null : (
          <>
            <span className="text-sky-500">{d.created}</span>
            {d.resolved > 0 && (
              <>
                <span className="text-muted-foreground">+</span>
                <span className="text-emerald-500">{d.resolved}</span>
              </>
            )}
          </>
        ),
      segments: [
        { value: d.created, className: "bg-sky-500/70" },
        { value: d.resolved, className: "bg-emerald-500/50" },
      ],
      // Hatched, not empty: nothing ran, so there is nothing to report rather
      // than nothing to find.
      blind: d.sweeps === 0 && total === 0,
      description: describe(d),
      link: isLinked(d) ? { to: "/insights", name: linkName(d) } : undefined,
    };
  });

  return (
    <DayChart
      days={chartDays}
      height={height}
      legend={[
        { swatch: "bg-sky-500/70", label: "New" },
        { swatch: "bg-emerald-500/50", label: "Resolved" },
      ]}
      blindLabel="No investigations"
      hint="Select a day to list its insights."
      note={note}
    />
  );
}

/** Whether the day's insights list has rows: something found, or a recorded
 *  resolution. An estimated one is not on the list. */
function isLinked(day: TrendDay): boolean {
  return day.created > 0 || day.resolvedRecorded > 0;
}

/** One day as a sentence, for the tooltip and the a11y label. */
function describe(day: TrendDay): string {
  if (day.investigations === 0 && day.created + day.resolved === 0) {
    return `${day.day}: no investigations`;
  }
  return `${day.day}: ${day.created} new, ${day.resolved} resolved, ${sweeps(day)}`;
}

/**
 * A linked day's accessible name. It starts with the day as the axis prints
 * it, so it contains the visible label (WCAG 2.5.3), strided-out columns
 * included; then the insights it opens, so the purpose is in the name
 * (WCAG 2.4.4); then what selecting it does.
 */
function linkName(day: TrendDay): string {
  return (
    `${dayLabel(day.day)}, ${day.day}: ${day.created} new and ${day.resolved} resolved ` +
    `insights (${sweeps(day)}). Show this day's insights`
  );
}

/** Every investigation, and apart the ones that recorded nothing, as the
 *  Activity chart says it, so the two charts give one number for a day. */
function sweeps(day: TrendDay): string {
  const ran = `${day.investigations} investigation${day.investigations === 1 ? "" : "s"}`;
  return day.unmeasured ? `${ran}, ${day.unmeasured} recorded nothing` : ran;
}
