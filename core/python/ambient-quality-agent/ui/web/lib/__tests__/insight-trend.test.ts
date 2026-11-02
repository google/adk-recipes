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

/**
 * The two ways an issue chart can lie.
 *
 * It can compress time — `/api/daily` omits days with no sweeps, so mapping its
 * rows straight to columns turns a fortnight with three sweeps into three
 * adjacent days. And it can date a resolution it never recorded, which is why
 * the estimate is reported as one.
 */
import { describe, expect, it } from "vitest";

import type { Day, InsightView } from "../aqua-api";
import { dayKey } from "../day-buckets";
import {
  buildTrend,
  resolutionDate,
  resolutionIsEstimated,
  resolvedNote,
} from "../insight-trend";

const NOW = new Date("2026-09-16T09:00:00Z");

const day = (over: Partial<Day> & { day: string }): Day => ({
  traces_evaluated: 0,
  traces_eval_passed: 0,
  traces_eval_failed: 0,
  traces_eval_errored: 0,
  ...over,
});

const insight = (over: Partial<InsightView>): InsightView =>
  ({
    insight_id: "i1",
    agent_name: "travel_desk_agent",
    label: "an issue",
    tool_name: "",
    status: "RESOLVED",
    occurrence_count: 1,
    trace_count: 1,
    impact: 1,
    last_run_at: null,
    diagnosis: "",
    confidence: 0,
    source_refs: [],
    ...over,
  }) as InsightView;

describe("buildTrend", () => {
  it("draws a column per day of the span, not per row returned", () => {
    const trend = buildTrend({
      days: [
        day({ day: "2026-09-16", insights_created: 2, investigations: 4 }),
      ],
      resolved: [],
      autoResolveDays: null,
      span: 5,
      now: NOW,
    });
    expect(trend).toHaveLength(5);
    expect(trend.map((t) => t.day)).toEqual([
      "2026-09-12",
      "2026-09-13",
      "2026-09-14",
      "2026-09-15",
      "2026-09-16",
    ]);
  });

  it("marks a day the server sent no row for as having no sweeps", () => {
    // The distinction the whole chart rests on: nothing ran, so there is
    // nothing to report rather than nothing to find.
    const trend = buildTrend({
      days: [
        day({ day: "2026-09-16", insights_created: 1, investigations: 2 }),
      ],
      resolved: [],
      autoResolveDays: null,
      span: 3,
      now: NOW,
    });
    expect(trend[0]).toMatchObject({
      day: "2026-09-14",
      sweeps: 0,
      created: 0,
    });
    expect(trend[2]).toMatchObject({
      day: "2026-09-16",
      sweeps: 2,
      created: 1,
    });
  });

  it("keeps a day that swept and found nothing apart from one that did not sweep", () => {
    const trend = buildTrend({
      days: [
        day({ day: "2026-09-15", insights_created: 0, investigations: 3 }),
      ],
      resolved: [],
      autoResolveDays: null,
      span: 2,
      now: NOW,
    });
    expect(trend[0]).toMatchObject({ created: 0, sweeps: 3 });
    expect(trend[1]).toMatchObject({ created: 0, sweeps: 0 });
  });

  it("treats a day whose every sweep was unmeasured as having no sweeps", () => {
    const trend = buildTrend({
      days: [
        day({
          day: "2026-09-15",
          insights_created: 0,
          investigations: 1,
          unmeasured: 1,
          traces_evaluated: 0,
        }),
      ],
      resolved: [],
      autoResolveDays: null,
      span: 2,
      now: NOW,
    });
    expect(trend[0]).toMatchObject({
      day: "2026-09-15",
      created: 0,
      sweeps: 0,
    });
  });

  it("yields zero sweeps when evaluated is zero and unmeasured is positive even if totalSweeps > unmeasured", () => {
    const trend = buildTrend({
      days: [
        day({
          day: "2026-09-15",
          insights_created: 0,
          investigations: 2,
          unmeasured: 1,
          traces_evaluated: 0,
        }),
      ],
      resolved: [],
      autoResolveDays: null,
      span: 2,
      now: NOW,
    });
    expect(trend[0]).toMatchObject({
      day: "2026-09-15",
      created: 0,
      sweeps: 0,
    });
  });

  it("discounts unmeasured sweeps on days that evaluated traces", () => {
    const trend = buildTrend({
      days: [
        day({
          day: "2026-09-15",
          insights_created: 1,
          investigations: 3,
          unmeasured: 1,
          traces_evaluated: 5,
        }),
      ],
      resolved: [],
      autoResolveDays: null,
      span: 2,
      now: NOW,
    });
    expect(trend[0]).toMatchObject({
      day: "2026-09-15",
      created: 1,
      sweeps: 2,
    });
  });

  it("counts a resolved issue on the day it was resolved", () => {
    const trend = buildTrend({
      days: [day({ day: "2026-09-15", investigations: 1 })],
      resolved: [insight({ resolved_at: "2026-09-15T12:00:00Z" })],
      autoResolveDays: 14,
      span: 3,
      now: NOW,
    });
    expect(trend.find((t) => t.day === "2026-09-15")?.resolved).toBe(1);
  });

  it("counts apart the resolutions it had to estimate", () => {
    // Only a recorded resolved_at is one the day's insights list can find.
    const trend = buildTrend({
      days: [],
      resolved: [
        insight({ insight_id: "stamped", resolved_at: "2026-09-15T12:00:00Z" }),
        insight({
          insight_id: "guessed",
          resolved_at: null,
          updated_at: "2026-09-01T00:00:00Z",
        }),
      ],
      autoResolveDays: 14,
      span: 3,
      now: NOW,
    });
    expect(trend.find((t) => t.day === "2026-09-15")).toMatchObject({
      resolved: 2,
      resolvedRecorded: 1,
    });
  });

  it("carries every investigation of the day and the ones that recorded nothing", () => {
    // The Activity chart counts them this way, so both charts give one number.
    const trend = buildTrend({
      days: [
        day({
          day: "2026-09-15",
          investigations: 4,
          unmeasured: 1,
          traces_evaluated: 5,
        }),
      ],
      resolved: [],
      autoResolveDays: null,
      span: 2,
      now: NOW,
    });
    expect(trend[0]).toMatchObject({
      investigations: 4,
      unmeasured: 1,
      sweeps: 3,
    });
  });

  it("drops a resolution that falls outside the span rather than clamping it", () => {
    const trend = buildTrend({
      days: [],
      resolved: [insight({ resolved_at: "2026-01-01T00:00:00Z" })],
      autoResolveDays: 14,
      span: 3,
      now: NOW,
    });
    expect(trend.reduce((n, t) => n + t.resolved, 0)).toBe(0);
  });
});

describe("resolutionDate", () => {
  it("uses the recorded date when there is one", () => {
    const d = resolutionDate(
      insight({ resolved_at: "2026-09-10T00:00:00Z" }),
      14,
    );
    expect(dayKey(d)).toBe("2026-09-10");
  });

  it("estimates from the last sighting plus the auto-resolve window", () => {
    // The row predates `resolved_at`; the most that can be said is that some
    // sweep closed it after the window elapsed.
    const d = resolutionDate(
      insight({ resolved_at: null, updated_at: "2026-09-01T00:00:00Z" }),
      14,
    );
    expect(dayKey(d)).toBe("2026-09-15");
  });

  it("refuses to guess when auto-resolution is off", () => {
    expect(
      resolutionDate(
        insight({ resolved_at: null, updated_at: "2026-09-01T00:00:00Z" }),
        null,
      ),
    ).toBeNull();
  });

  it("refuses to guess when there is no sighting to count from", () => {
    expect(resolutionDate(insight({ resolved_at: null }), 14)).toBeNull();
  });
});

describe("the honesty note", () => {
  it("says nothing when every resolution was recorded", () => {
    expect(
      resolvedNote([insight({ resolved_at: "2026-09-10T00:00:00Z" })], 14),
    ).toBe("");
  });

  it("says so when any date had to be inferred", () => {
    const some = [
      insight({ resolved_at: "2026-09-10T00:00:00Z" }),
      insight({
        insight_id: "i2",
        resolved_at: null,
        updated_at: "2026-09-01T00:00:00Z",
      }),
    ];
    expect(resolutionIsEstimated(some)).toBe(true);
    expect(resolvedNote(some, 14)).toContain("accurate to one sweep");
  });

  it("explains an always-empty series rather than leaving it blank", () => {
    expect(resolvedNote([], null)).toBe(
      "Auto-resolution is disabled, so nothing resolves.",
    );
  });
});
