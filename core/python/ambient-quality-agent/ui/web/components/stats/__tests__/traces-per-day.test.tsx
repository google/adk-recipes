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
 * Three states, three different claims about the agent.
 *
 * A day nothing swept, a day whose sweeps all recorded nothing, and a day that
 * measured and found zero failures all used to draw as the same empty column.
 * Only the last of those is a statement about quality.
 */
import { render, screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { TracesPerDay } from "../traces-per-day";
import { renderInRouter } from "./render-in-router";
import type { Day } from "@/lib/aqua-api";

const NOW = new Date("2026-09-16T09:00:00Z");

const day = (over: Partial<Day> & { day: string }): Day => ({
  traces_evaluated: 0,
  traces_eval_passed: 0,
  traces_eval_failed: 0,
  traces_eval_errored: 0,
  ...over,
});

describe("TracesPerDay", () => {
  it("draws a column for every day of the span, not per row returned", () => {
    render(
      <TracesPerDay
        days={[day({ day: "2026-09-16", investigations: 1 })]}
        span={5}
        now={NOW}
      />,
    );
    expect(screen.getAllByRole("img")).toHaveLength(5);
  });

  it("says a day with no sweeps had none", () => {
    render(<TracesPerDay days={[]} span={2} now={NOW} />);
    expect(
      screen.getByRole("img", { name: "2026-09-15: no investigations" }),
    ).toBeInTheDocument();
  });

  it("reports a day that swept and measured, even when nothing failed", () => {
    render(
      <TracesPerDay
        days={[
          day({
            day: "2026-09-16",
            investigations: 2,
            traces_evaluated: 8,
            traces_eval_passed: 8,
          }),
        ]}
        span={1}
        now={NOW}
      />,
    );
    expect(
      screen.getByRole("img", {
        name: "2026-09-16: 8 evaluated, 8 passed, 0 failed; 2 investigations",
      }),
    ).toBeInTheDocument();
  });

  it("names the sweeps that recorded nothing, rather than counting them as zero", () => {
    // Three runs that failed or are still going measured nothing; the day is
    // as blind as one with no runs, and the tooltip says which.
    render(
      <TracesPerDay
        days={[day({ day: "2026-09-16", investigations: 3, unmeasured: 3 })]}
        span={1}
        now={NOW}
      />,
    );
    expect(
      screen.getByRole("img", {
        name: "2026-09-16: 0 evaluated, 0 passed, 0 failed; 3 investigations, 3 recorded nothing",
      }),
    ).toBeInTheDocument();
  });

  it("totals each series in the legend", () => {
    render(
      <TracesPerDay
        days={[
          day({
            day: "2026-09-15",
            investigations: 1,
            traces_evaluated: 5,
            traces_eval_passed: 4,
            traces_eval_failed: 1,
          }),
          day({
            day: "2026-09-16",
            investigations: 1,
            traces_evaluated: 5,
            traces_eval_passed: 3,
            traces_eval_failed: 2,
          }),
        ]}
        span={2}
        now={NOW}
      />,
    );
    expect(screen.getByText("7 passed")).toBeInTheDocument();
    expect(screen.getByText("3 failed")).toBeInTheDocument();
  });

  it("mentions eval errors only when there were some", () => {
    const { rerender } = render(
      <TracesPerDay
        days={[day({ day: "2026-09-16", investigations: 1 })]}
        span={1}
        now={NOW}
      />,
    );
    expect(screen.queryByText(/eval service errors/)).toBeNull();

    rerender(
      <TracesPerDay
        days={[
          day({ day: "2026-09-16", investigations: 1, traces_eval_errored: 4 }),
        ]}
        span={1}
        now={NOW}
      />,
    );
    expect(screen.getByText("4 eval service errors")).toBeInTheDocument();
  });

  it("draws plain columns unless asked to link them, and offers no selection", () => {
    render(
      <TracesPerDay
        days={[day({ day: "2026-09-16", investigations: 1 })]}
        span={1}
        now={NOW}
      />,
    );
    expect(screen.queryByRole("link")).toBeNull();
    expect(screen.queryByText(/Select a day/)).toBeNull();
  });
});

/** The chart as Home draws it: inside a router, with its days linked. */
const renderLinked = (days: Day[], span: number) =>
  renderInRouter(<TracesPerDay days={days} span={span} now={NOW} linkDays />);

describe("TracesPerDay with its days linked", () => {
  const measured = day({
    day: "2026-09-15",
    investigations: 4,
    traces_evaluated: 10,
    traces_eval_passed: 8,
    traces_eval_failed: 2,
  });

  it("links a day with investigations to that day's list", async () => {
    await renderLinked([measured], 2);
    const link = screen.getByRole("link");
    expect(link).toHaveAttribute("href", "/investigations?day=2026-09-15");
  });

  it("links a day whose investigations all recorded nothing, since they are listed", async () => {
    await renderLinked(
      [day({ day: "2026-09-16", investigations: 3, unmeasured: 3 })],
      1,
    );
    expect(screen.getByRole("link")).toHaveAttribute(
      "href",
      "/investigations?day=2026-09-16",
    );
  });

  it("leaves a day with no investigations plain", async () => {
    await renderLinked([measured], 2);
    // 2026-09-16 has no row, so nothing to open.
    expect(screen.getAllByRole("link")).toHaveLength(1);
    expect(
      screen
        .getByRole("img", { name: "2026-09-16: no investigations" })
        .closest("a"),
    ).toBeNull();
  });

  it("names the link by its visible day, then what it opens, then what for", async () => {
    await renderLinked(
      [
        { ...measured, unmeasured: 1, traces_eval_errored: 1 },
        day({ day: "2026-09-16", investigations: 1 }),
      ],
      2,
    );
    expect(
      screen.getByRole("link", {
        name:
          "15 Tue, 2026-09-15: 4 investigations, 1 recorded nothing; 10 trajectories evaluated, " +
          "8 passed, 2 failed, 1 eval service error. Show this day's investigations",
      }),
    ).toBeInTheDocument();
    // The link is its role; saying "click" repeats it, and names one device.
    expect(document.body.innerHTML).not.toMatch(/click/i);
  });

  it("keeps the link outside the chart image, which only describes", async () => {
    await renderLinked([measured], 2);
    for (const img of screen.getAllByRole("img")) {
      expect(within(img).queryByRole("link")).toBeNull();
    }
  });

  it("does not read a linked day's figures twice, and keeps its tooltip", async () => {
    await renderLinked([measured], 2);
    const link = screen.getByRole("link");
    expect(within(link).queryByRole("img")).toBeNull();
    expect(
      within(link).getByTitle(
        "2026-09-15: 10 evaluated, 8 passed, 2 failed; 4 investigations",
      ),
    ).toBeInTheDocument();
    // The plain day beside it is still an image with its sentence.
    expect(
      screen.getByRole("img", { name: "2026-09-16: no investigations" }),
    ).toBeInTheDocument();
  });

  it("shows keyboard focus inside the column, where the chart cannot clip it", async () => {
    await renderLinked([measured], 2);
    expect(screen.getByRole("link")).toHaveClass(
      "focus-visible:outline-none",
      "focus-visible:ring-2",
      "focus-visible:ring-ring",
      "focus-visible:ring-inset",
      "rounded-sm",
    );
  });

  it("says at rest that a day can be selected", async () => {
    await renderLinked([measured], 2);
    expect(
      screen.getByText("Select a day to list its investigations."),
    ).toBeInTheDocument();
  });

  it("tints a hovered day enough to see in both themes", async () => {
    await renderLinked([measured], 2);
    expect(screen.getByRole("link")).toHaveClass("hover:bg-foreground/10");
  });

  it("pads linked and plain columns alike, so the bars stay level", async () => {
    await renderLinked([measured], 2);
    const plain = screen.getByRole("img", {
      name: "2026-09-16: no investigations",
    }).parentElement!.parentElement!;
    expect(screen.getByRole("link")).toHaveClass("py-0.5");
    expect(plain).toHaveClass("py-0.5");
  });

  it("keeps each linked day 24px wide by scrolling the row, not squeezing the days", async () => {
    // WCAG 2.5.8 below ~470px; the columns still shrink as #311 needs.
    await renderLinked([measured], 14);
    const link = screen.getByRole("link");
    const row = link.parentElement!;
    expect(row.style.minWidth).toBe("21rem");
    expect(row.parentElement).toHaveClass("overflow-x-auto");
    expect(link).toHaveClass("min-w-0", "flex-1");
  });

  it("fits a day label to its column's whole pitch, bold today included", async () => {
    // Centred in a column, a label is as wide as its text unless capped, and
    // "16 Wed" in bold then spills out of the last column and scrolls the row.
    // Capped at the content box, "13 Sat" truncates even at 1440px; the
    // padding is there to space the bars, so the label may use it.
    await renderLinked([measured], 2);
    for (const label of [
      screen.getByText("15 Tue"),
      screen.getByText("16 Wed"),
    ]) {
      expect(label).toHaveClass(
        "truncate",
        "-mx-1",
        "max-w-[calc(100%+0.5rem)]",
      );
    }
  });

  it("sets no floor on a chart without links", () => {
    render(<TracesPerDay days={[measured]} span={14} now={NOW} />);
    const row = screen
      .getByRole("img", { name: /^2026-09-15/ })
      .closest(".flex-1")!.parentElement!;
    expect(row.style.minWidth).toBe("");
  });

  it("keeps linked and plain columns the same shrinkable width", async () => {
    await renderLinked([measured], 2);
    const link = screen.getByRole("link");
    const plain = screen.getByRole("img", {
      name: "2026-09-16: no investigations",
    }).parentElement!.parentElement!;
    expect(link).toHaveClass("min-w-0", "flex-1");
    expect(plain).toHaveClass("min-w-0", "flex-1");
  });
});
