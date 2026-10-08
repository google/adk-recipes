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

import { Link } from "@tanstack/react-router";

import { isLabelled, labelStride } from "@/lib/day-buckets";
import type { ListPage } from "@/lib/funnel";
import { textStyle } from "@/lib/typography";
import { cn } from "@/lib/utils";
import { DAY_LINK, linkedRowFloor } from "./day-link";

/** Diagonal stripes for a day nothing measured: thin and faint, so an absence
 *  reads as background rather than competing with the measured bars. */
const NO_SWEEP_HATCH = {
  backgroundImage:
    "repeating-linear-gradient(45deg, hsl(var(--muted-foreground) / 0.2) 0 1px, transparent 1px 5px)",
} as const;

/** One stacked part of a day's bar. */
export interface Segment {
  value: number;
  /** Its fill; empty leaves the part unfilled. */
  className: string;
}

/** One day of a per-day chart, in the chart's own terms. */
export interface ChartDay {
  /** `YYYY-MM-DD`, UTC. */
  day: string;
  isToday: boolean;
  /** The day's figure above its bar, or null for none. */
  figure: React.ReactNode;
  /** Top first. */
  segments: Segment[];
  /** What the bar's height stands for, when the segments do not add up to it
   *  (the rest of the bar stays unfilled). Defaults to their sum. */
  total?: number;
  /** Nothing was measured: hatched, which a zero bar would misstate. */
  blind: boolean;
  /** The day as a sentence, for the tooltip and the a11y label. */
  description: string;
  /** Where selecting the day goes and the link's accessible name; absent for
   *  a day with nothing to open. */
  link?: { to: ListPage; name: string };
}

export interface LegendItem {
  label: React.ReactNode;
  /** The swatch's fill; absent for an entry with no colour of its own. */
  swatch?: string;
}

/**
 * The per-day bar chart every day chart draws, so they look and read alike.
 *
 * Not a new chart: this is the drawing half of `TracesPerDay`, which since
 * #263 draws both Activity and Investigations, lifted out so `InsightTrend`
 * draws with it too instead of keeping its own copy. `lib/day-buckets.ts`
 * decides which days a chart shows; this decides how a day looks. Each chart
 * keeps only the mapping from its own rows (a traces day, an insights day) to
 * `ChartDay`.
 *
 * Each column is a figure, a bar of fixed height, and the day as the axis
 * prints it. The bar is one of three things, and they are three claims:
 * hatched when nothing was measured, a hairline when something was measured
 * and came to zero, and the stacked segments otherwise.
 *
 * A column with a `link` opens that day's list, and the hint below the legend
 * says so; a column without one stays plain, having nothing to open.
 */
export function DayChart({
  days,
  height = "h-40",
  legend,
  blindLabel,
  hint,
  note,
}: {
  days: readonly ChartDay[];
  height?: string;
  legend: readonly LegendItem[];
  /** The hatch's legend entry. */
  blindLabel: string;
  /** Shown when some day links, since nothing else marks them selectable
   *  before a pointer finds one. */
  hint: string;
  note?: string;
}) {
  const anyLinked = days.some((d) => d.link);
  const peak = Math.max(1, ...days.map(totalOf));

  return (
    <div className="flex w-full min-w-0 flex-col gap-2 overflow-hidden">
      {/* The columns touch and pad their bars apart instead of a gap, so a
          linked column's target is the whole pitch rather than the bar. */}
      <div className="min-w-0 overflow-x-auto">
        <div
          className="flex w-full min-w-0 items-end"
          style={anyLinked ? linkedRowFloor(days.length) : undefined}
        >
          {days.map((d, i) => (
            <Column
              key={d.day}
              day={d}
              peak={peak}
              height={height}
              showLabel={isLabelled(i, days.length)}
              labelFit={labelFit(i, days.length)}
            />
          ))}
        </div>
      </div>

      <div className={cn(textStyle.meta, "flex flex-wrap items-center gap-4")}>
        {legend.map((item, i) => (
          // biome-ignore lint/suspicious/noArrayIndexKey: legend labels are React nodes, and each chart passes a fixed list.
          <span key={i} className="flex items-center gap-1.5">
            {item.swatch && (
              <span
                className={cn("h-2 w-2 rounded-sm", item.swatch)}
                aria-hidden="true"
              />
            )}
            {item.label}
          </span>
        ))}
        <span className="flex items-center gap-1.5">
          <span
            className="h-2 w-2 rounded-sm border border-border"
            style={NO_SWEEP_HATCH}
            aria-hidden="true"
          />
          {blindLabel}
        </span>
      </div>
      {anyLinked && <p className={textStyle.meta}>{hint}</p>}
      {note && <p className={textStyle.meta}>{note}</p>}
    </div>
  );
}

function Column({
  day,
  peak,
  height,
  showLabel,
  labelFit,
}: {
  day: ChartDay;
  peak: number;
  height: string;
  showLabel: boolean;
  labelFit: string;
}) {
  const total = totalOf(day);
  const link = day.link;
  // Padded on every side, plain columns too so the bars stay level, which
  // keeps a linked day's focus ring off its figure and label.
  const columnClass =
    "flex min-w-0 flex-1 flex-col items-center gap-1 px-1 py-0.5";

  const content = (
    <>
      <span className={cn(textStyle.meta, "text-foreground")}>
        {day.figure ?? "\u00a0"}
      </span>
      {/* The fixed height belongs to the plot, not the column: the bar's
          height is a percentage, and a percentage of an auto-height parent is
          zero. */}
      <div className={cn("flex w-full items-end", height)}>
        {/* biome-ignore lint/a11y/useAriaPropsSupportedByRole: the label is set only together with role="img"; a linked day drops both. */}
        <div
          className="flex w-full flex-col justify-end"
          style={{
            height: day.blind ? "100%" : `${(total / peak) * 100}%`,
            minHeight: 1,
          }}
          // Linked, the link's name already says all of this, so the track
          // keeps only the tooltip.
          role={link ? undefined : "img"}
          aria-label={link ? undefined : day.description}
          title={day.description}
        >
          {day.blind ? (
            <span className="h-full w-full rounded-sm" style={NO_SWEEP_HATCH} />
          ) : total === 0 ? (
            <span className="h-px w-full bg-border" />
          ) : (
            // Rounded here rather than on the track: when the segments fall
            // short of `total`, the track's top is unfilled, and rounding it
            // would leave the drawn top square.
            <span
              className="flex w-full flex-col overflow-hidden rounded-sm"
              style={{ height: `${(drawnOf(day) / total) * 100}%` }}
            >
              {day.segments.map((s, i) => (
                <span
                  // biome-ignore lint/suspicious/noArrayIndexKey: a day's segments are a fixed stack with no ids.
                  key={i}
                  className={cn("w-full", s.className)}
                  style={{ flexGrow: s.value, minHeight: s.value ? 2 : 0 }}
                />
              ))}
            </span>
          )}
        </div>
      </div>
      <span
        className={cn(
          textStyle.meta,
          labelFit,
          day.isToday
            ? "font-medium text-foreground"
            : "text-muted-foreground group-hover:text-foreground",
        )}
      >
        {showLabel ? dayLabel(day.day) : "\u00a0"}
      </span>
    </>
  );

  return link ? (
    <Link
      to={link.to}
      search={{ day: day.day }}
      aria-label={link.name}
      className={cn(columnClass, "group", DAY_LINK)}
    >
      {content}
    </Link>
  ) : (
    <div className={columnClass}>{content}</div>
  );
}

/**
 * How a column's day label may use the room around it.
 *
 * With every column labelled, a label gets its own pitch and truncates past
 * it. Strided, the columns beside a label are blank, so it keeps its whole
 * text and spills into them; at either end of the row it hugs the edge
 * instead, since the row's scroll box would clip it there.
 */
function labelFit(index: number, count: number): string {
  if (labelStride(count) === 1)
    return "-mx-1 max-w-[calc(100%+0.5rem)] truncate";
  if (index === count - 1) return "self-end whitespace-nowrap";
  if (index === 0) return "self-start whitespace-nowrap";
  return "whitespace-nowrap";
}

function drawnOf(day: ChartDay): number {
  return day.segments.reduce((sum, s) => sum + s.value, 0);
}

function totalOf(day: ChartDay): number {
  return day.total ?? drawnOf(day);
}

/** A day as the axis prints it, "16 Wed". A linked day's accessible name
 *  starts with it, so the name contains the visible label (WCAG 2.5.3). */
export function dayLabel(iso: string): string {
  const d = new Date(`${iso}T00:00:00Z`);
  return `${d.getUTCDate()} ${["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"][d.getUTCDay()]}`;
}
