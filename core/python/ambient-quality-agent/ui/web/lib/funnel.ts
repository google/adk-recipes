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

/** The pipeline funnel's shape: which stages there are, and where the ribbons
 * between them run.
 *
 * Pure, and separate from the component, because the ribbon maths is the part
 * worth testing: the component's own job is to measure the DOM, which jsdom
 * cannot do.
 */

import type { Stats } from "./aqua-api";

/** A stacked part of one stage's bar. `tone` is the fill, shared with the
 * legend swatch so the two cannot drift. */
export interface SegmentSpec {
  key: keyof Stats;
  label: string;
  tone: string;
}

/** A stage's stable name, for anything keyed to a stage: its label is display
 * text and gets reworded. */
export type StageId = "window" | "ingested" | "evaluated" | "insights";

/** A page that lists what a chart counts. */
export type ListPage = "/investigations" | "/insights";

export interface StageSpec {
  id: StageId;
  label: string;
  /** The page that lists what this stage counts. */
  href: ListPage;
  segments: SegmentSpec[];
}

/** Named for what the volume did, not for whether it was good news: the
 * colours track the stream, so sky carries on to the next stage, violet
 * carries on degraded, and anything that fell out of the pipeline is red.
 * Passed traces end here rather than continuing, hence teal and not more sky;
 * failed ones are the only stream into insights, so they warm towards it. */
export const STAGES: readonly StageSpec[] = [
  {
    id: "window",
    label: "Trajectories in the window",
    href: "/investigations",
    segments: [
      { key: "traces_scanned", label: "in the window", tone: "bg-sky-500" },
    ],
  },
  {
    id: "ingested",
    label: "Ingested",
    href: "/investigations",
    segments: [
      { key: "traces_ingested", label: "ingested", tone: "bg-sky-500" },
      {
        key: "traces_ingested_partial",
        label: "partially ingested",
        tone: "bg-violet-500",
      },
      {
        key: "traces_ingested_failed",
        label: "ingestion failed",
        tone: "bg-red-500",
      },
    ],
  },
  {
    id: "evaluated",
    label: "Evaluated",
    href: "/investigations",
    segments: [
      { key: "traces_eval_passed", label: "passed", tone: "bg-teal-400" },
      { key: "traces_eval_failed", label: "failed", tone: "bg-orange-400" },
      {
        key: "traces_eval_errored",
        label: "eval service errors",
        tone: "bg-red-500",
      },
    ],
  },
  {
    id: "insights",
    label: "Insights",
    href: "/insights",
    segments: [
      { key: "insights_created", label: "new", tone: "bg-rose-400" },
      { key: "insights_recurring", label: "recurring", tone: "bg-amber-300" },
    ],
  },
];

/** What each stage counts, for the reader who asks. A definition of the stage
 * and of its total only: the bar's legend already names the segments. Each
 * sentence restates a counter's definition in `InvestigationCounters`
 * (tools/investigations/models.py), so a change there is a change here. */
export const STAGE_HELP: Readonly<Record<StageId, string>> = {
  window:
    "Trajectories in each investigation's time window, before sampling, " +
    "added up across investigations. The next stages work on a sample of " +
    "these.",
  ingested:
    "Sampled trajectories that the system tried to load for analysis. This " +
    "total includes trajectories whose ingestion failed.",
  evaluated:
    "Ingested trajectories that metrics checked, to see how the agent " +
    "performed.",
  insights:
    "Failure patterns found in the evaluated trajectories. One insight can " +
    "count more than once, even in one investigation, so this is not the " +
    "number of distinct insights.",
};

/** STAGE_HELP where it differs for a funnel of one investigation, whose
 * window is its own rather than a sum over many. */
export const STAGE_HELP_ONE_RUN: Readonly<Partial<Record<StageId, string>>> = {
  window:
    "Trajectories in this investigation's time window, before sampling. The " +
    "next stages work on a sample of these.",
};

/** What flows from one stage into the next. Each link names the *part* of the
 * source bar that feeds the target, which is the thing the bars alone cannot
 * say: evaluation takes the ingested traces but not the dropped ones, and only
 * the failing evaluations go on to produce findings. `span` returns that part
 * as `[from, to]` offsets in units, measured from the source bar's left edge. */
export interface LinkSpec {
  from: number;
  to: number;
  span: (s: Stats) => [number, number];
}

export const LINKS: readonly LinkSpec[] = [
  // Everything the window held is what sampling drew from.
  { from: 0, to: 1, span: (s) => [0, count(s.traces_scanned)] },
  // Ingested and partly-ingested traces are evaluated; the dropped ones,
  // stacked last, are where this ribbon stops short of the bar's end.
  {
    from: 1,
    to: 2,
    span: (s) => [
      0,
      count(s.traces_ingested) + count(s.traces_ingested_partial),
    ],
  },
  // Only failures reach clustering, so the ribbon starts after the passed
  // segment and ends before the eval errors.
  {
    from: 2,
    to: 3,
    span: (s) => [
      count(s.traces_eval_passed),
      count(s.traces_eval_passed) + count(s.traces_eval_failed),
    ],
  },
];

/** A counter as a non-negative number. An engine that predates a counter omits
 * it, and `undefined` in the arithmetic turns a whole bar into NaN%. */
export function count(value: number | undefined): number {
  const n = Number(value);
  return Number.isFinite(n) && n > 0 ? n : 0;
}

/** The three evaluation outcomes, off either a run's counters or the totals. */
export interface EvalOutcomes {
  traces_eval_passed?: number;
  traces_eval_failed?: number;
  traces_eval_errored?: number;
}

/** Traces whose evaluation reached an outcome — what the Evaluated stage
 * totals, and the one definition of the word anywhere it is shown.
 *
 * Not `traces_evaluated`, which counts cases sent to the eval service: a case
 * with neither verdicts nor a score lands in none of the three, so the sent
 * count is the larger number and splitting it by outcome leaves a gap. Not
 * `metrics_*` either, which count `(case, metric)` pairs and are larger again. */
export function evaluatedTraces(c: EvalOutcomes): number {
  return (
    count(c.traces_eval_passed) +
    count(c.traces_eval_failed) +
    count(c.traces_eval_errored)
  );
}

export interface Segment extends SegmentSpec {
  value: number;
  /** Share of its own stage, as a CSS width. */
  width: string;
}

export interface Stage {
  id: StageId;
  label: string;
  href: ListPage;
  help: string;
  total: number;
  /** Share of the widest stage, as a CSS width. This is what makes the bars
   * shrink down the funnel. */
  width: string;
  /** Only the non-zero ones: a clean run should read as a short bar rather
   * than a row of noughts. */
  segments: Segment[];
  ariaLabel: string;
}

/** The four stages, measured against one shared scale.
 *
 * The shared scale is the point. Each stage passes on less than it received --
 * most traces are never sampled, some are dropped, most evaluations pass and
 * produce nothing -- and drawing each bar against its own total would hide that
 * entirely, showing four full-width bars for a pipeline that discards most of
 * what it starts with.
 *
 * The cost, accepted deliberately: the last bar can be a percent or two of the
 * first, so its split is a sliver. Every figure is written out beside the bar
 * and in its legend, and a floor keeps a stage that produced something from
 * rendering as nothing at all -- which would read as "this stage did not run".
 */
export function layoutFunnel(stats: Stats): { stages: Stage[]; scale: number } {
  const totals = STAGES.map((stage) =>
    stage.segments.reduce((sum, seg) => sum + count(stats[seg.key]), 0),
  );
  const scale = Math.max(1, ...totals);

  const stages = STAGES.map((stage, i) => {
    const total = totals[i];
    const segments = stage.segments
      .map((seg) => ({
        ...seg,
        value: count(stats[seg.key]),
        width: total ? `${(count(stats[seg.key]) / total) * 100}%` : "0%",
      }))
      .filter((seg) => seg.value > 0);
    return {
      id: stage.id,
      label: stage.label,
      href: stage.href,
      help:
        (stats.investigations === 1 && STAGE_HELP_ONE_RUN[stage.id]) ||
        STAGE_HELP[stage.id],
      total,
      width: total ? `${Math.max(0.4, (total / scale) * 100)}%` : "100%",
      segments,
      ariaLabel: [
        `${stage.label}: ${total.toLocaleString()} total.`,
        segments
          .map((s) => `${s.value.toLocaleString()} ${s.label}`)
          .join(", "),
      ]
        .filter(Boolean)
        .join(" "),
    };
  });

  return { stages, scale };
}

/** Where a bar sits on the shared scale, as `[left%, right%]`; centred, so a
 * bar of width w spans `(100-w)/2` to `(100+w)/2`. */
export function barExtent(total: number, scale: number): [number, number] {
  const width = Math.max(0, (total / scale) * 100);
  return [(100 - width) / 2, (100 + width) / 2];
}

export interface Ribbon {
  key: string;
  /** The shaded body of the ribbon, as an SVG path. */
  area: string;
  /** Its two edges, which are what the eye actually follows. */
  edges: [string, string];
}

/**
 * The ribbons joining each stage to the next, in an SVG whose x axis *is* the
 * shared scale: a viewBox of `0 0 100 height` with `preserveAspectRatio="none"`
 * makes an x coordinate literally a percentage of the panel, so the ribbons
 * reflow with the window and no width ever has to be measured.
 *
 * Only the vertical positions come from the DOM, since those depend on how the
 * labels and legends wrapped. `bounds[i]` is stage i's bar as `[top, bottom]`
 * in pixels, relative to the container.
 */
export function ribbons(
  stats: Stats,
  stages: readonly Stage[],
  scale: number,
  bounds: readonly (readonly [number, number])[],
): Ribbon[] {
  if (bounds.length !== stages.length) return [];

  return LINKS.flatMap((link) => {
    const sourceTotal = stages[link.from].total;
    const targetTotal = stages[link.to].total;
    if (!sourceTotal || !targetTotal) return [];

    // Top of the gap is the bottom of the source bar, bottom of it the top of
    // the target bar -- so the ribbon spans exactly the space between them,
    // whatever the text in between did.
    const y1 = bounds[link.from][1];
    const y2 = bounds[link.to][0];
    if (y2 <= y1) return [];
    const mid = (y1 + y2) / 2;

    const [sourceLeft] = barExtent(sourceTotal, scale);
    const [from, to] = link.span(stats);
    const x1 = sourceLeft + (from / scale) * 100;
    const x2 = sourceLeft + (to / scale) * 100;
    const [tx1, tx2] = barExtent(targetTotal, scale);

    const edge = (ax: number, bx: number) =>
      `M ${ax} ${y1} C ${ax} ${mid}, ${bx} ${mid}, ${bx} ${y2}`;

    return [
      {
        key: `${link.from}-${link.to}`,
        area: `${edge(x1, tx1)} L ${tx2} ${y2} C ${tx2} ${mid}, ${x2} ${mid}, ${x2} ${y1} Z`,
        edges: [edge(x1, tx1), edge(x2, tx2)] as [string, string],
      },
    ];
  });
}
