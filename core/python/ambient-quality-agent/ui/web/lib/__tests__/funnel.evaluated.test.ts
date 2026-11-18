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

// biome-ignore-all lint/style/noNonNullAssertion: a missing value fails the test either way; the assertion only narrows the type.

/**
 * "Evaluated" has one meaning across the dashboard. These pin the number the
 * lists print to the number the funnel's Evaluated bar draws.
 */
import { describe, expect, it } from "vitest";
import { withStatsDefaults } from "../aqua-api";
import { countEvaluatedTrajectories, layoutFunnel } from "../funnel";

describe("countEvaluatedTrajectories", () => {
  it("is the funnel's Evaluated bar, for the same figures", () => {
    const figures = {
      trajectories_evaluated: 500,
      trajectories_eval_passed: 300,
      trajectories_eval_failed: 120,
      trajectories_eval_errored: 30,
    };

    const { stages } = layoutFunnel(withStatsDefaults(figures));
    const evaluatedBar = stages.find((s) => s.id === "evaluated")!;

    expect(countEvaluatedTrajectories(figures)).toBe(evaluatedBar.total);
    expect(countEvaluatedTrajectories(figures)).toBe(450);
  });

  it("is not trajectories_evaluated, which counts what was sent to the service", () => {
    // A case with neither verdicts nor a score reaches none of the three, so
    // the sent count runs ahead of the outcomes.
    expect(
      countEvaluatedTrajectories({
        trajectories_eval_passed: 10,
        trajectories_eval_failed: 1,
      }),
    ).toBe(11);
  });

  it("reads an absent counter as zero rather than NaN", () => {
    expect(countEvaluatedTrajectories({})).toBe(0);
    expect(
      countEvaluatedTrajectories({ trajectories_eval_passed: undefined }),
    ).toBe(0);
  });

  it("ignores a negative counter rather than subtracting it", () => {
    expect(
      countEvaluatedTrajectories({
        trajectories_eval_passed: 5,
        trajectories_eval_failed: -3,
      }),
    ).toBe(5);
  });
});
