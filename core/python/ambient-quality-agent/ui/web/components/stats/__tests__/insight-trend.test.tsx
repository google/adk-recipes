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
 * A day nothing swept must not read as a day that found nothing.
 *
 * Both are a column of zero height, and only one of them is a statement about
 * the agent. The hatch and the label are what keep them apart, so they are
 * pinned here rather than left to the eye.
 */
import { render, screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { InsightTrend } from "../insight-trend";
import { renderInRouter } from "./render-in-router";
import type { TrendDay } from "@/lib/insight-trend";

/** A day whose resolutions were all recorded and whose sweeps all measured. */
const d = (over: Partial<TrendDay> & { day: string }): TrendDay => ({
  isToday: false,
  created: 0,
  resolved: 0,
  resolvedRecorded: over.resolved ?? 0,
  sweeps: 0,
  investigations: over.sweeps ?? 0,
  unmeasured: 0,
  ...over,
});

describe("InsightTrend", () => {
  it("says a day with no sweeps has none, rather than reporting zero issues", () => {
    render(<InsightTrend days={[d({ day: "2026-09-14" })]} />);
    expect(
      screen.getByRole("img", { name: "2026-09-14: no investigations" }),
    ).toBeInTheDocument();
  });

  it("reports a day that swept and found nothing as exactly that", () => {
    render(<InsightTrend days={[d({ day: "2026-09-15", sweeps: 3 })]} />);
    expect(
      screen.getByRole("img", {
        name: "2026-09-15: 0 new, 0 resolved, 3 investigations",
      }),
    ).toBeInTheDocument();
  });

  it("renders bars instead of no-sweep hatch when issues were resolved on a zero-sweep day", async () => {
    await renderTrend([d({ day: "2026-09-15", resolved: 2, sweeps: 0 })]);
    expect(
      screen.getByTitle("2026-09-15: 0 new, 2 resolved, 0 investigations"),
    ).toBeInTheDocument();
  });

  it("labels both series on a busy day", async () => {
    await renderTrend([
      d({ day: "2026-09-16", created: 2, resolved: 1, sweeps: 4 }),
    ]);
    expect(
      screen.getByTitle("2026-09-16: 2 new, 1 resolved, 4 investigations"),
    ).toBeInTheDocument();
  });

  it("carries the honesty note when the resolution dates were inferred", () => {
    render(
      <InsightTrend
        days={[d({ day: "2026-09-16" })]}
        note="accurate to one sweep"
      />,
    );
    expect(screen.getByText("accurate to one sweep")).toBeInTheDocument();
  });

  it("names its series, so the two colours do not have to be decoded", () => {
    render(<InsightTrend days={[d({ day: "2026-09-16", sweeps: 1 })]} />);
    expect(screen.getByText("New")).toBeInTheDocument();
    expect(screen.getByText("Resolved")).toBeInTheDocument();
    expect(screen.getByText("No investigations")).toBeInTheDocument();
  });

  it("strides date labels on a 14-day span so columns fit without overflowing", async () => {
    // Every other day linked: a link column must shrink like a plain one.
    const days = Array.from({ length: 14 }, (_, i) => {
      const dayNum = String(i + 5).padStart(2, "0");
      return d({ day: `2026-09-${dayNum}`, sweeps: 1, created: i % 2 });
    });
    await renderTrend(days);
    const columns = document.querySelectorAll(".flex-1.min-w-0");
    expect(columns).toHaveLength(14);
    expect(screen.getAllByRole("link")).toHaveLength(7);

    const labels = Array.from(columns).map(
      (col) => col.lastElementChild?.textContent,
    );
    expect(labels).toContain("18 Fri");
    expect(labels).toContain("\u00a0");
  });

  it("links a day where something was found or resolved to that day's insights", async () => {
    await renderTrend([
      d({ day: "2026-09-14", created: 1, sweeps: 2 }),
      d({ day: "2026-09-15", resolved: 2, sweeps: 0 }),
    ]);
    expect(
      screen.getAllByRole("link").map((l) => l.getAttribute("href")),
    ).toEqual(["/insights?day=2026-09-14", "/insights?day=2026-09-15"]);
  });

  it("leaves a day with nothing found or resolved plain, swept or not", async () => {
    await renderTrend([
      d({ day: "2026-09-14", created: 1, sweeps: 2 }),
      d({ day: "2026-09-15", sweeps: 3 }),
      d({ day: "2026-09-16" }),
    ]);
    expect(screen.getAllByRole("link")).toHaveLength(1);
  });

  it("leaves a day plain when its only resolution was estimated, which the list cannot find", async () => {
    await renderTrend([
      d({ day: "2026-09-15", resolved: 1, resolvedRecorded: 0, sweeps: 2 }),
    ]);
    expect(screen.queryByRole("link")).toBeNull();
  });

  it("counts a day's investigations as the Activity chart does", () => {
    render(
      <InsightTrend
        days={[
          d({ day: "2026-09-18", sweeps: 3, investigations: 4, unmeasured: 1 }),
        ]}
      />,
    );
    expect(
      screen.getByTitle(
        "2026-09-18: 0 new, 0 resolved, 4 investigations, 1 recorded nothing",
      ),
    ).toBeInTheDocument();
  });

  it("prints above a column what its link opens: the new and the resolved", async () => {
    await renderTrend([
      d({ day: "2026-09-20", created: 3, resolved: 1, sweeps: 4 }),
      d({ day: "2026-09-21", created: 1, sweeps: 4 }),
      d({ day: "2026-09-22", resolved: 1, sweeps: 4 }),
    ]);
    const figures = Array.from(
      document.querySelectorAll(".flex-1.min-w-0"),
    ).map((col) => col.firstElementChild?.textContent);
    expect(figures).toEqual(["3+1", "1", "0+1"]);
  });

  it("names the link by its visible day, then what it opens, then what for", async () => {
    await renderTrend([
      d({
        day: "2026-09-16",
        created: 2,
        resolved: 1,
        sweeps: 3,
        investigations: 4,
        unmeasured: 1,
      }),
    ]);
    expect(
      screen.getByRole("link", {
        name:
          "16 Wed, 2026-09-16: 2 new and 1 resolved insights (4 investigations, 1 recorded nothing). " +
          "Show this day's insights",
      }),
    ).toBeInTheDocument();
    expect(document.body.innerHTML).not.toMatch(/click/i);
  });

  it("says at rest that a day can be selected, only when one can", async () => {
    await renderTrend([
      d({ day: "2026-09-15", sweeps: 1 }),
      d({ day: "2026-09-16", created: 2, sweeps: 1 }),
    ]);
    expect(
      screen.getByText("Select a day to list its insights."),
    ).toBeInTheDocument();
  });

  it("keeps each linked day 24px wide by scrolling the row, not squeezing the days", async () => {
    const days = Array.from({ length: 14 }, (_, i) =>
      d({
        day: `2026-09-${String(i + 5).padStart(2, "0")}`,
        sweeps: 1,
        created: i % 2,
      }),
    );
    await renderTrend(days);
    const row = screen.getAllByRole("link")[0].parentElement!;
    expect(row.style.minWidth).toBe("21rem");
    expect(row.parentElement).toHaveClass("overflow-x-auto");
  });

  it("fits a day label to its column's whole pitch", () => {
    render(<InsightTrend days={[d({ day: "2026-09-15", sweeps: 1 })]} />);
    expect(screen.getByText("15 Tue")).toHaveClass(
      "truncate",
      "-mx-1",
      "max-w-[calc(100%+0.5rem)]",
    );
  });

  it("offers no selection when no day links", () => {
    render(<InsightTrend days={[d({ day: "2026-09-15", sweeps: 1 })]} />);
    expect(screen.queryByText(/Select a day/)).toBeNull();
  });

  it("keeps the link outside the chart image, which only describes", async () => {
    await renderTrend([
      d({ day: "2026-09-15", sweeps: 1 }),
      d({ day: "2026-09-16", created: 2, sweeps: 1 }),
    ]);
    for (const img of screen.getAllByRole("img")) {
      expect(within(img).queryByRole("link")).toBeNull();
    }
  });

  it("does not read a linked day's figures twice, and keeps its tooltip", async () => {
    await renderTrend([
      d({ day: "2026-09-15", sweeps: 1 }),
      d({ day: "2026-09-16", created: 2, sweeps: 1 }),
    ]);
    const link = screen.getByRole("link");
    expect(within(link).queryByRole("img")).toBeNull();
    expect(
      within(link).getByTitle("2026-09-16: 2 new, 0 resolved, 1 investigation"),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("img", {
        name: "2026-09-15: 0 new, 0 resolved, 1 investigation",
      }),
    ).toBeInTheDocument();
  });

  it("shows keyboard focus inside the column, where the chart cannot clip it", async () => {
    await renderTrend([d({ day: "2026-09-16", created: 2, sweeps: 1 })]);
    expect(screen.getByRole("link")).toHaveClass(
      "focus-visible:outline-none",
      "focus-visible:ring-2",
      "focus-visible:ring-ring",
      "focus-visible:ring-inset",
      "rounded-sm",
    );
  });
});

const renderTrend = (days: TrendDay[]) =>
  renderInRouter(<InsightTrend days={days} />);
