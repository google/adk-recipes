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

import { useCallback, useLayoutEffect, useRef, useState } from "react";
import { Link } from "@tanstack/react-router";
import { layoutFunnel, ribbons, type ListPage, type Stage } from "@/lib/funnel";
import type { Stats } from "@/lib/aqua-api";
import { HelpPopover } from "@/components/ui/help-popover";
import { link, textStyle } from "@/lib/typography";
import { cn } from "@/lib/utils";

const NOTE =
  "All four are drawn to one scale, so each bar shows how much of the " +
  "previous stage reached it. The first three count trajectories; insights counts " +
  "each investigation's new and recurring insights, added up: an insight counts " +
  "again in every investigation that records it, so this is not the number of " +
  "distinct insights.";

/** NOTE for the funnel of one investigation, which adds nothing up. */
const ONE_RUN_NOTE =
  "All four are drawn to one scale, so each bar shows how much of the " +
  "previous stage reached it. The first three count trajectories; insights counts " +
  "this investigation's new and recurring insights, and one insight can count " +
  "more than once, so this is not the number of distinct insights.";

/** Spoken after a linked stage's title: three stages open one list, and no
 * title says which list that is. */
const DESTINATION: Record<ListPage, string> = {
  "/investigations": "view investigations",
  "/insights": "view insights",
};

/** Where each bar sits vertically, in pixels relative to the funnel, so the
 * ribbons can be drawn into the gaps between them. */
interface Geometry {
  height: number;
  bounds: [number, number][];
}

function sameGeometry(a: Geometry | null, b: Geometry): boolean {
  return (
    a !== null &&
    a.height === b.height &&
    a.bounds.length === b.bounds.length &&
    a.bounds.every((v, i) => v[0] === b.bounds[i][0] && v[1] === b.bounds[i][1])
  );
}

/**
 * The pipeline as a funnel: four stacked bars, centred, on one shared scale,
 * with ribbons showing which part of each stage fed the next.
 *
 * One component for every view that draws this, because two reads of
 * `/api/stats` can straddle a finishing sweep: a second funnel written to match
 * is a second funnel that can end up disagreeing with this one.
 *
 * `linkStages` makes each stage's title a link to the page that lists what the
 * stage counts. Off by default, since a view that already is that page would
 * link to itself.
 *
 * The note and each stage's (i) say which they describe: totals added up over
 * many investigations, or, when `stats` counts one, that investigation alone.
 */
export function Funnel({
  stats,
  className,
  linkStages = false,
}: {
  stats: Stats;
  className?: string;
  linkStages?: boolean;
}) {
  const { stages, scale } = layoutFunnel(stats);
  const containerRef = useRef<HTMLDivElement>(null);
  const trackRefs = useRef<(HTMLDivElement | null)[]>([]);
  const [geometry, setGeometry] = useState<Geometry | null>(null);

  // Measured off bounding rects rather than `offsetTop`: the rows are
  // positioned, to sit above the overlay, so each one is its own offset parent
  // and every track would report the same tiny offset within its row.
  const measure = useCallback(() => {
    const container = containerRef.current;
    const tracks = trackRefs.current.slice(0, stages.length);
    if (
      !container ||
      tracks.length !== stages.length ||
      tracks.some((t) => !t)
    ) {
      return;
    }
    const origin = container.getBoundingClientRect().top;
    const next: Geometry = {
      height: container.clientHeight,
      bounds: tracks.map((track) => {
        // biome-ignore lint/style/noNonNullAssertion: the guard above returns when any track is missing.
        const rect = track!.getBoundingClientRect();
        return [rect.top - origin, rect.bottom - origin] as [number, number];
      }),
    };
    // Bailing on an unchanged measurement is what keeps the observer from
    // feeding itself: a re-render that measures the same thing stops here.
    setGeometry((prev) => (sameGeometry(prev, next) ? prev : next));
  }, [stages.length]);

  // The vertical positions depend on how the labels and legends wrapped, so
  // the ribbons are re-measured whenever the panel changes shape.
  useLayoutEffect(() => {
    measure();
    const container = containerRef.current;
    if (!container || typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(() => measure());
    observer.observe(container);
    return () => observer.disconnect();
  }, [measure]);

  const paths = geometry ? ribbons(stats, stages, scale, geometry.bounds) : [];

  return (
    <div className={cn("flex flex-col gap-2", className)}>
      <div ref={containerRef} className="relative flex flex-col gap-2">
        {/* Behind the rows, so the light edges pass under the legend text
            rather than over it, and inert to the pointer. */}
        {paths.length > 0 && geometry && (
          <svg
            className="pointer-events-none absolute inset-0 z-0 h-full w-full"
            viewBox={`0 0 100 ${geometry.height}`}
            preserveAspectRatio="none"
            aria-hidden="true"
          >
            {paths.map((ribbon) => (
              <g key={ribbon.key}>
                <path
                  d={ribbon.area}
                  fill="hsl(var(--border))"
                  fillOpacity={0.28}
                />
                {ribbon.edges.map((d) => (
                  <path
                    key={d}
                    d={d}
                    fill="none"
                    stroke="hsl(var(--border))"
                    strokeWidth={1}
                    // Keeps the edges 1px however far the x axis is stretched.
                    vectorEffect="non-scaling-stroke"
                  />
                ))}
              </g>
            ))}
          </svg>
        )}

        {stages.map((stage, i) => (
          <FunnelBar
            key={stage.id}
            stage={stage}
            linked={linkStages}
            trackRef={(node) => {
              trackRefs.current[i] = node;
            }}
          />
        ))}
      </div>
      <p className={textStyle.meta}>
        {stats.investigations === 1 ? ONE_RUN_NOTE : NOTE}
      </p>
    </div>
  );
}

function FunnelBar({
  stage,
  linked,
  trackRef,
}: {
  stage: Stage;
  linked: boolean;
  trackRef: (node: HTMLDivElement | null) => void;
}) {
  const empty = stage.total === 0 || stage.segments.length === 0;
  const labelClass = cn(textStyle.label, "truncate");

  return (
    <div className="relative z-10 min-w-0">
      <div className="flex items-baseline justify-between gap-2.5">
        <span className="flex min-w-0 items-center gap-1">
          {linked ? (
            <Link
              to={stage.href}
              className={cn(
                labelClass,
                link.standalone,
                "hover:text-foreground focus-visible:ring-inset",
              )}
            >
              {stage.label}
              <span className="sr-only">, {DESTINATION[stage.href]}</span>
            </Link>
          ) : (
            <span className={labelClass}>{stage.label}</span>
          )}
          <HelpPopover topic={`the ${stage.label} stage`}>
            {stage.help}
          </HelpPopover>
        </span>
        {/* Hidden, as is the legend: the image's label already reads it. */}
        <span aria-hidden="true" className={textStyle.itemTitle}>
          {stage.total.toLocaleString()}
        </span>
      </div>

      {/* Centred, so the shrinking stages form a funnel rather than a
          left-aligned staircase. A stage that measured nothing gets a hairline
          across the full width instead of an invisible bar, which would read
          as "this stage did not run". The image is the track alone: the
          children of an img are presentational, so the title link must sit
          outside it. */}
      <div
        ref={trackRef}
        role="img"
        aria-label={stage.ariaLabel}
        className={cn(
          "mx-auto flex overflow-hidden rounded-sm",
          empty ? "mt-2.5 h-[3px] bg-border" : "mb-[5px] mt-1 h-4",
        )}
        style={{ width: stage.width, minWidth: 3 }}
      >
        {!empty &&
          stage.segments.map((seg) => (
            <span
              key={seg.key}
              className={cn("h-full min-w-[2px]", seg.tone)}
              style={{ width: seg.width }}
              title={`${seg.value.toLocaleString()} ${seg.label}`}
            />
          ))}
      </div>

      {/* A bar with nothing to split gets no legend: the total above it already
          says the number, and repeating it under a swatch is three copies of
          one figure. Hidden from assistive tech, which gets the same figures
          from the image's label. */}
      {stage.segments.length > 1 && (
        <div
          aria-hidden="true"
          className={cn(
            textStyle.meta,
            "flex flex-wrap justify-center gap-x-3 gap-y-0.5",
          )}
        >
          {stage.segments.map((seg) => (
            <span key={seg.key} className="inline-flex items-center gap-1.5">
              <span className={cn("h-2 w-2 shrink-0 rounded-sm", seg.tone)} />
              <span className="font-medium text-foreground">
                {seg.value.toLocaleString()}
              </span>
              {seg.label}
            </span>
          ))}
        </div>
      )}
    </div>
  );
}
