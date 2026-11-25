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
import { counters } from "../__fixtures__/counters";
import {
  formatInsightReach,
  formatPickedSignal,
  formatShare,
  hasEstimates,
  listPickedFor,
} from "../estimates";

describe("hasEstimates", () => {
  it("accepts an investigation that recorded estimates", () => {
    expect(
      hasEstimates(
        counters({
          trajectories_evaluated: 10,
          estimated_trajectories_eval_passed: 30,
          estimated_trajectories_eval_failed: 10,
        }),
      ),
    ).toBe(true);
  });

  it("refuses a record without estimates", () => {
    expect(hasEstimates(counters({ trajectories_evaluated: 10 }))).toBe(false);
    expect(hasEstimates(undefined)).toBe(false);
  });
});

describe("formatShare", () => {
  it("keeps one decimal for small shares and none for large ones", () => {
    expect(formatShare(0.012)).toBe("1.2%");
    expect(formatShare(0.05)).toBe("5%");
    expect(formatShare(0.234)).toBe("23%");
  });

  it("never shows more than all conversations", () => {
    expect(formatShare(1.2)).toBe("100%");
  });

  it("does not round a rare defect to nothing", () => {
    expect(formatShare(0.0004)).toBe("<0.1%");
    expect(formatShare(0)).toBe("0%");
  });
});

describe("formatInsightReach", () => {
  it("puts the share before the reviewed count", () => {
    expect(formatInsightReach("1,200", 0.012, 1200)).toBe(
      "about 1.2% of conversations · 1,200 reviewed",
    );
  });

  it("counts trajectories without an estimate", () => {
    expect(formatInsightReach("12", null, 12)).toBe("12 trajectories");
    expect(formatInsightReach("1", undefined, 1)).toBe("1 trajectory");
  });
});

describe("listPickedFor", () => {
  const none = {
    has_error: false,
    has_abnormal_finish: false,
    has_tool_reported_error: false,
    busy_turn_tail: 0,
    cost_tail: 0,
    latency_tail: 0,
  };

  it("names each signal that raised the weight, strongest first", () => {
    expect(
      listPickedFor({
        has_error: true,
        has_abnormal_finish: true,
        has_tool_reported_error: true,
        busy_turn_percentile: 0.97,
        cost_percentile: 0.95,
        latency_percentile: 1,
        busy_turn_tail: 0.7,
        cost_tail: 0.5,
        latency_tail: 1,
      }).map(formatPickedSignal),
    ).toEqual([
      "Failed call",
      "Cut-off response",
      "Tool reported failure",
      "Repeated tool calls (top 3%)",
      "High token usage (top 5%)",
      "Slow turn (slowest in window)",
    ]);
  });

  it("names the window's highest value rather than a rank", () => {
    expect(
      listPickedFor({
        ...none,
        busy_turn_percentile: 1,
        cost_percentile: 1,
        latency_percentile: 0.99,
        busy_turn_tail: 1,
        cost_tail: 1,
        latency_tail: 0.9,
      }).map(formatPickedSignal),
    ).toEqual([
      "Repeated tool calls (most in window)",
      "High token usage (most in window)",
      "Slow turn (top 1%)",
    ]);
  });

  it("explains every signal and ranks only the measured ones", () => {
    const picked = listPickedFor({
      has_error: true,
      has_abnormal_finish: true,
      has_tool_reported_error: true,
      busy_turn_percentile: 0.97,
      cost_percentile: 0.95,
      latency_percentile: 0.92,
      busy_turn_tail: 0.7,
      cost_tail: 0.5,
      latency_tail: 0.2,
    });

    expect(new Set(picked.map((signal) => signal.key)).size).toBe(6);
    for (const signal of picked) {
      expect(signal.description).toMatch(/\.$/);
    }
    expect(picked.filter((signal) => signal.rank).length).toBe(3);
  });

  it("leaves out a measure ranked high that raised no weight", () => {
    expect(listPickedFor({ ...none, cost_percentile: 0.85 })).toEqual([]);
  });

  it("is empty for a trajectory picked without a signal", () => {
    expect(listPickedFor(none)).toEqual([]);
    expect(listPickedFor(null)).toEqual([]);
  });
});
