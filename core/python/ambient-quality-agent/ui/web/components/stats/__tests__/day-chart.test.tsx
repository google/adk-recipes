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

import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { InsightTrend } from "../insight-trend";
import { TracesPerDay } from "../traces-per-day";
import type { Day } from "@/lib/aqua-api";
import type { TrendDay } from "@/lib/insight-trend";

const NOW = new Date("2026-09-16T12:00:00Z");

const measured: Day = {
  day: "2026-09-16",
  investigations: 1,
  traces_evaluated: 0,
  traces_eval_passed: 0,
  traces_eval_failed: 0,
  traces_eval_errored: 0,
} as Day;

const trendDay: TrendDay = {
  day: "2026-09-16",
  isToday: true,
  created: 0,
  resolved: 0,
  resolvedRecorded: 0,
  sweeps: 1,
  investigations: 1,
  unmeasured: 0,
};

/** The column's bar, its day label, and the classes each is drawn with. */
function anatomy(): string[] {
  const bar = screen.getByRole("img");
  const column = bar.parentElement!.parentElement!;
  return [
    column.className,
    bar.parentElement!.className,
    bar.className,
    bar.firstElementChild!.className,
    column.lastElementChild!.className,
    column.lastElementChild!.textContent!,
  ];
}

describe("the per-day charts", () => {
  it("draw a measured zero day the same way", () => {
    const { unmount } = render(
      <TracesPerDay days={[measured]} span={1} now={NOW} height="h-20" />,
    );
    const activity = anatomy();
    unmount();
    render(<InsightTrend days={[trendDay]} height="h-20" />);
    expect(anatomy()).toEqual(activity);
    // A day that looked and found nothing is a hairline, not an empty track.
    expect(activity[3]).toContain("h-px");
    expect(activity[5]).toBe("16 Wed");
  });

  it("keep a strided label whole, hugging the row's end", () => {
    const days = Array.from({ length: 14 }, (_, i) => ({
      ...trendDay,
      day: `2026-09-${String(i + 15).padStart(2, "0")}`,
      isToday: i === 13,
    }));
    render(<InsightTrend days={days} />);
    const today = screen.getByText("28 Mon");
    expect(today).not.toHaveClass("truncate");
    expect(today).toHaveClass("whitespace-nowrap", "self-end");
    expect(screen.getByText("26 Sat")).not.toHaveClass("truncate", "self-end");
  });
});
