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
 * The chart bug that reads as good news.
 *
 * `/api/daily` returns a row only for days that swept, so a chart drawn from
 * those rows compresses time — and the compressed version is *more*
 * reassuring than the truth: a fortnight with three quiet days renders as an
 * unbroken run of activity. These pin the columns to the calendar instead.
 */
import { describe, expect, it } from "vitest";

import {
  alignToDays,
  DASHBOARD_DAYS,
  dayKey,
  dayWindow,
  isLabelled,
  labelStride,
  parseDay,
  windowStart,
} from "../day-buckets";

const NOW = new Date("2026-09-16T09:00:00Z");

describe("dayKey", () => {
  it("buckets in UTC, to agree with the server's DATE(window_end)", () => {
    // 23:30Z is still the 15th; a local bucket east of Greenwich would file it
    // under the 16th while the totals counted it on the 15th.
    expect(dayKey("2026-09-15T23:30:00Z")).toBe("2026-09-15");
  });

  it("is null for anything it cannot parse", () => {
    expect(dayKey(null)).toBeNull();
    expect(dayKey("")).toBeNull();
    expect(dayKey("not a date")).toBeNull();
  });
});

describe("alignToDays", () => {
  it("emits one column per day of the span, oldest first, ending today", () => {
    const cols = alignToDays([], { span: 4, now: NOW });
    expect(cols.map((c) => c.day)).toEqual([
      "2026-09-13",
      "2026-09-14",
      "2026-09-15",
      "2026-09-16",
    ]);
  });

  it("leaves a day the server skipped with no row at all", () => {
    // Not a zero row: "nothing swept" and "swept and measured zero" are
    // different claims, and only the absent row can carry the first.
    const cols = alignToDays([{ day: "2026-09-16", n: 1 }], {
      span: 3,
      now: NOW,
    });
    expect(cols.map((c) => c.row)).toEqual([
      null,
      null,
      { day: "2026-09-16", n: 1 },
    ]);
  });

  it("does not compress a sparse fortnight into a solid run", () => {
    const rows = [
      { day: "2026-09-05" },
      { day: "2026-09-10" },
      { day: "2026-09-16" },
    ];
    const cols = alignToDays(rows, { span: 14, now: NOW });
    expect(cols).toHaveLength(14);
    expect(cols.filter((c) => c.row !== null)).toHaveLength(3);
  });

  it("drops a row outside the span rather than clamping it into the edge", () => {
    const cols = alignToDays([{ day: "2026-01-01" }], { span: 3, now: NOW });
    expect(cols.every((c) => c.row === null)).toBe(true);
  });

  it("marks today, so the highlight cannot land on the wrong column", () => {
    const cols = alignToDays([], { span: 3, now: NOW });
    expect(cols.filter((c) => c.isToday).map((c) => c.day)).toEqual([
      "2026-09-16",
    ]);
  });
});

describe("windowStart", () => {
  it("opens at midnight on the span's first day, not at this hour 14 days ago", () => {
    // `now - span` would open the window mid-morning, so the oldest column
    // would hold only the sweeps after 09:00 and read as a quiet day.
    expect(windowStart(4, NOW)).toBe("2026-09-13T00:00:00.000Z");
  });

  it("names the same first day the leftmost column does", () => {
    // One number, two readers: a window that opened a day off the axis would
    // draw a column the server was never asked about.
    const cols = alignToDays([], { span: DASHBOARD_DAYS, now: NOW });
    expect(windowStart(DASHBOARD_DAYS, NOW)).toBe(
      `${cols[0].day}T00:00:00.000Z`,
    );
  });
});

describe("labelStride", () => {
  it("labels every column while they still fit", () => {
    expect(labelStride(10)).toBe(1);
    expect([0, 5, 9].every((i) => isLabelled(i, 10))).toBe(true);
  });

  it("thins them out above the limit", () => {
    expect(labelStride(30)).toBe(3);
  });

  it("always labels the most recent day, whatever the stride", () => {
    // Counted from the right: that column is the one a reader looks for.
    for (const count of [7, 14, 21, 30, 31]) {
      expect(isLabelled(count - 1, count)).toBe(true);
    }
  });
});

describe("parseDay", () => {
  it("keeps a real UTC day spelled YYYY-MM-DD", () => {
    expect(parseDay("2026-09-20")).toBe("2026-09-20");
    expect(parseDay("2024-02-29")).toBe("2024-02-29");
  });

  it("drops what only looks like one", () => {
    // Each would otherwise be a filter that matches nothing, under a chip
    // naming a day that does not exist.
    for (const value of [
      "2026-02-30",
      "2026-13-01",
      "2025-02-29",
      "2026-9-20",
      "2026-09-20T00:00:00Z",
      " 2026-09-20",
      "",
      "foo",
    ]) {
      expect(parseDay(value), String(value)).toBeUndefined();
    }
  });

  it("reads a number the way the URL parser hands it over", () => {
    // TanStack parses `?day=20260920` to a number; it is still not a day.
    expect(parseDay(20260920)).toBeUndefined();
    expect(parseDay(2026)).toBeUndefined();
    expect(parseDay(undefined)).toBeUndefined();
    expect(parseDay(null)).toBeUndefined();
  });
});

describe("dayWindow", () => {
  it("spans the whole UTC day, both ends inclusive", () => {
    expect(dayWindow("2026-09-20")).toEqual({
      windowStart: "2026-09-20T00:00:00.000Z",
      windowEnd: "2026-09-20T23:59:59.999999Z",
    });
  });
});
