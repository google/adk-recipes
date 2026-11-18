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

import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { ProgressBar } from "../investigation-progress";

const AT = "2026-11-02T10:00:00Z";

describe("ProgressBar", () => {
  it("glides to a sweep's new share and sweeps a highlight across it, unless motion is reduced", () => {
    render(
      <ProgressBar
        progress={{
          stage: "Session review",
          evaluated: 45,
          total: 205,
          updated_at: AT,
        }}
      />,
    );

    const bar = screen.getByRole("progressbar", { name: "Session review" });
    expect(bar).toHaveAttribute("aria-valuetext", "45 of 205 trajectories");
    const fill = bar.firstElementChild as HTMLElement;
    expect(fill).toHaveStyle({ width: `${(45 / 205) * 100}%` });
    expect(fill).toHaveClass(
      "transition-[width]",
      "motion-reduce:transition-none",
    );
    const sheen = fill.firstElementChild as HTMLElement;
    expect(sheen).toHaveAttribute("aria-hidden", "true");
    expect(sheen).toHaveClass(
      "hidden",
      "motion-safe:block",
      "motion-safe:animate-progress-sheen",
    );
  });

  it("runs a segment along the track for a stage with no total, and draws nothing where motion is reduced", () => {
    render(
      <ProgressBar
        progress={{ stage: "Clustering", findings: 12, updated_at: AT }}
      />,
    );

    const bar = screen.getByRole("progressbar", { name: "Clustering" });
    expect(bar).not.toHaveAttribute("aria-valuenow");
    expect(bar).toHaveAttribute("aria-valuetext", "12 findings");
    // A still bar would read as empty, so it is drawn only while it moves.
    expect(bar).toHaveClass("hidden", "motion-safe:block");
    expect(bar.firstElementChild).toHaveClass(
      "motion-safe:animate-progress-travel",
    );
  });

  it("runs a segment rather than a negative share for a total below zero", () => {
    render(
      <ProgressBar
        progress={{ stage: "Session review", total: -1, updated_at: AT }}
      />,
    );

    const bar = screen.getByRole("progressbar", { name: "Session review" });
    expect(bar).not.toHaveAttribute("aria-valuenow");
    expect(bar).not.toHaveAttribute("aria-valuemax");
  });
});
