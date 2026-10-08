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

import { describe, expect, it } from "vitest";
import { withStatsDefaults, type Stats } from "../aqua-api";
import {
  barExtent,
  layoutFunnel,
  ribbons,
  STAGE_HELP,
  STAGES,
} from "../funnel";

const stats = (over: Partial<Stats> = {}): Stats => withStatsDefaults(over);

/** Four bars, stacked with a 10px gap, each 16px tall. */
const BOUNDS: [number, number][] = [
  [0, 16],
  [26, 42],
  [52, 68],
  [78, 94],
];

describe("layoutFunnel", () => {
  it("draws every stage against the widest one, not against itself", () => {
    const { stages, scale } = layoutFunnel(
      stats({
        traces_scanned: 1000,
        traces_ingested: 100,
        traces_eval_passed: 80,
        traces_eval_failed: 20,
        insights_created: 3,
      }),
    );

    expect(scale).toBe(1000);
    expect(stages.map((s) => s.total)).toEqual([1000, 100, 100, 3]);
    expect(stages.map((s) => s.width)).toEqual(["100%", "10%", "10%", "0.4%"]);
  });

  it("gives a stage that produced something a floor rather than nothing", () => {
    // 1 of 100000 is 0.001%, which rounds to an invisible bar -- and an
    // invisible bar reads as "this stage did not run".
    const { stages } = layoutFunnel(
      stats({ traces_scanned: 100_000, insights_created: 1 }),
    );
    expect(stages[3].width).toBe("0.4%");
  });

  // Keyed on the stage id, not its label: a renamed label must not move a link.
  it("sends each stage to the page that lists what it counts", () => {
    const { stages } = layoutFunnel(stats());
    expect(Object.fromEntries(stages.map((s) => [s.id, s.href]))).toEqual({
      window: "/investigations",
      ingested: "/investigations",
      evaluated: "/investigations",
      insights: "/insights",
    });
  });

  it("explains every stage, keyed by its id", () => {
    expect(Object.keys(STAGE_HELP).sort()).toEqual(
      STAGES.map((s) => s.id).sort(),
    );
    const { stages } = layoutFunnel(stats());
    expect(stages.map((s) => s.help)).toEqual(
      STAGES.map((s) => STAGE_HELP[s.id]),
    );
  });

  // Ingested totals the sample, not the window, and includes failed ingestions.
  it("says the Ingested total is the sample, failed ingestions included", () => {
    expect(STAGE_HELP.ingested).toMatch(/sampled/i);
    expect(STAGE_HELP.ingested).toMatch(
      /includes trajectories whose ingestion failed/,
    );
  });

  // The legend already names each segment, so the help defines the stage and
  // its total instead of listing the segments again.
  it("defines each stage without listing the segments its legend shows", () => {
    for (const stage of STAGES) {
      for (const seg of stage.segments) {
        if (stage.id === "ingested" && seg.key === "traces_ingested_failed")
          continue;
        if (seg.label === stage.label.toLowerCase()) continue;
        expect(STAGE_HELP[stage.id]).not.toMatch(
          new RegExp(`\\b${seg.label}\\b`, "i"),
        );
      }
    }
    for (const text of Object.values(STAGE_HELP))
      expect(text).not.toMatch(/dropped/i);
  });

  it("says an insight can count more than once, so the total is not distinct", () => {
    expect(STAGE_HELP.insights).toMatch(
      /more than once, even in one investigation/,
    );
    expect(STAGE_HELP.insights).toMatch(/not the number of distinct insights/);
  });

  // A glance, not a paragraph.
  it("keeps each stage's help to 30 words", () => {
    for (const text of Object.values(STAGE_HELP)) {
      expect(text.split(/\s+/).length).toBeLessThanOrEqual(30);
    }
  });

  // Only what the bar counts: nothing about what the pipeline left out of it.
  it("says nothing about clusters the verification pass withholds", () => {
    expect(STAGE_HELP.insights).not.toMatch(/withh|verification/i);
    expect(STAGE_HELP.insights).not.toMatch(/mint/i);
  });

  it("keeps a stage that measured nothing full-width and empty", () => {
    const { stages } = layoutFunnel(stats({ traces_scanned: 10 }));
    expect(stages[1].total).toBe(0);
    expect(stages[1].width).toBe("100%");
    expect(stages[1].segments).toEqual([]);
  });

  it("drops zero segments so a clean run is not a row of noughts", () => {
    const { stages } = layoutFunnel(
      stats({
        traces_ingested: 50,
        traces_ingested_partial: 0,
        traces_ingested_failed: 2,
      }),
    );
    expect(stages[1].segments.map((s) => s.label)).toEqual([
      "ingested",
      "ingestion failed",
    ]);
    expect(stages[1].segments.map((s) => s.width)).toEqual([
      `${(50 / 52) * 100}%`,
      `${(2 / 52) * 100}%`,
    ]);
  });

  it("totals the Evaluated stage from its outcomes, not from traces_evaluated", () => {
    // `traces_evaluated` counts cases sent to the eval service; a case with
    // neither verdicts nor a score lands in none of the three outcomes, so
    // splitting the sent count by them would leave an unexplained gap.
    const { stages } = layoutFunnel(
      stats({
        traces_evaluated: 100,
        traces_eval_passed: 60,
        traces_eval_failed: 30,
        traces_eval_errored: 5,
      }),
    );
    expect(stages[2].total).toBe(95);
  });

  it("survives an engine that omits a counter entirely", () => {
    const { stages } = layoutFunnel({ traces_scanned: 10 } as Stats);
    expect(stages.every((s) => Number.isFinite(s.total))).toBe(true);
    expect(stages.every((s) => !s.width.includes("NaN"))).toBe(true);
  });

  it("names the split in the a11y label so the bar is readable without colour", () => {
    const { stages } = layoutFunnel(
      stats({ insights_created: 2, insights_recurring: 7 }),
    );
    expect(stages[3].ariaLabel).toBe("Insights: 9 total. 2 new, 7 recurring");
  });
});

describe("barExtent", () => {
  it("centres a bar, so the stages form a funnel and not a staircase", () => {
    expect(barExtent(50, 100)).toEqual([25, 75]);
    expect(barExtent(100, 100)).toEqual([0, 100]);
  });
});

describe("ribbons", () => {
  const full = stats({
    traces_scanned: 1000,
    traces_ingested: 80,
    traces_ingested_partial: 10,
    traces_ingested_failed: 10,
    traces_eval_passed: 60,
    traces_eval_failed: 30,
    traces_eval_errored: 5,
    insights_created: 4,
    insights_recurring: 1,
  });

  it("joins each stage to the next", () => {
    const { stages, scale } = layoutFunnel(full);
    const paths = ribbons(full, stages, scale, BOUNDS);
    expect(paths.map((r) => r.key)).toEqual(["0-1", "1-2", "2-3"]);
    expect(paths.every((r) => r.edges.length === 2)).toBe(true);
  });

  it("stops the ingest ribbon short of the dropped traces", () => {
    const { stages, scale } = layoutFunnel(full);
    const [, ingestToEval] = ribbons(full, stages, scale, BOUNDS);

    // The Ingested bar spans 100 units on a 1000 scale, centred: 45% to 55%.
    // 90 of those 100 carry on, so the ribbon leaves at 45% and ends at 54%.
    expect(ingestToEval.edges[0]).toContain("M 45 42");
    expect(ingestToEval.edges[1]).toContain("M 54 42");
  });

  it("takes only the failures into insights", () => {
    const { stages, scale } = layoutFunnel(full);
    const [, , evalToInsights] = ribbons(full, stages, scale, BOUNDS);

    // Evaluated spans 95 units centred: 45.25% to 54.75%. The ribbon starts
    // after the 60 passed and ends before the 5 errored.
    expect(evalToInsights.edges[0]).toContain("M 51.25 68");
    expect(evalToInsights.edges[1]).toContain("M 54.25 68");
  });

  it("draws nothing when a stage is empty", () => {
    const half = stats({ traces_scanned: 100, traces_ingested: 10 });
    const { stages, scale } = layoutFunnel(half);
    expect(ribbons(half, stages, scale, BOUNDS).map((r) => r.key)).toEqual([
      "0-1",
    ]);
  });

  it("draws nothing before anything has been measured", () => {
    // jsdom, and the first paint in a browser: every rect is zero, so every
    // gap is zero-height and a ribbon would be a smear across the bars.
    const { stages, scale } = layoutFunnel(full);
    const unmeasured: [number, number][] = [
      [0, 0],
      [0, 0],
      [0, 0],
      [0, 0],
    ];
    expect(ribbons(full, stages, scale, unmeasured)).toEqual([]);
    expect(ribbons(full, stages, scale, [])).toEqual([]);
  });
});
