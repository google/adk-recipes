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

// Which reads carry the dashboard's horizon, and which deliberately do not.
//
// Sending `windowStart` on all four scoped endpoints is the obvious version of
// this change and it is wrong: the two lists answer questions that are not
// about a period, and the insights endpoint windows on a field that would take
// the resolved series off the chart. These pin the split so the obvious version
// cannot be restored by someone tidying up.

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  dailyQuery,
  insightsQuery,
  runsQuery,
  statsQuery,
} from "@/lib/aqua-api";

const NOW = new Date("2026-09-16T09:00:00Z");

let fetchMock: ReturnType<typeof vi.fn>;

beforeEach(() => {
  vi.useFakeTimers();
  vi.setSystemTime(NOW);
  fetchMock = vi.fn(
    async () =>
      new Response(
        JSON.stringify({ stats: {}, days: [], runs: [], insights: [] }),
        {
          status: 200,
        },
      ),
  );
  vi.stubGlobal("fetch", fetchMock);
});

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

const requestedUrl = (): string => String(fetchMock.mock.calls[0][0]);

describe("the scoped reads", () => {
  it("sums the funnel over the horizon rather than over all time", async () => {
    await statsQuery().queryFn();
    const url = new URL(requestedUrl(), "http://x");
    expect(url.pathname).toBe("/api/stats");
    expect(url.searchParams.get("windowStart")).toBe(
      "2026-09-03T00:00:00.000Z",
    );
  });

  it("asks /api/daily for the days the charts draw, and no more", async () => {
    await dailyQuery().queryFn();
    const url = new URL(requestedUrl(), "http://x");
    expect(url.pathname).toBe("/api/daily");
    expect(url.searchParams.get("windowStart")).toBe(
      "2026-09-03T00:00:00.000Z",
    );
  });

  it("keeps the moving instant out of the query key", async () => {
    // The bound is computed in the query function, so the key stays stable and
    // the cache is worth having; a key holding the clock is a fresh query on
    // every render.
    const before = statsQuery().queryKey;
    vi.setSystemTime(new Date(NOW.getTime() + 3 * 86_400_000));
    expect(statsQuery().queryKey).toEqual(before);
    expect(JSON.stringify(before)).not.toContain("2026");

    await statsQuery().queryFn();
    const url = new URL(requestedUrl(), "http://x");
    expect(url.searchParams.get("windowStart")).toBe(
      "2026-09-06T00:00:00.000Z",
    );
  });
});

describe("the reads left unscoped", () => {
  it("asks for the most recent sweeps, not the last fortnight of them", async () => {
    // The server already caps this list; a window could only make it shorter,
    // and a log is read for depth.
    await runsQuery().queryFn();
    expect(requestedUrl()).toBe("/api/investigations");
  });

  it("never windows the issue list", async () => {
    // `windowStart` filters `updated_at` here — the last sighting. An
    // auto-resolved issue's last sighting is one auto-resolve window before the
    // day the chart plots it on, so a matching window would drop exactly the
    // resolutions being drawn.
    await insightsQuery().queryFn();
    expect(requestedUrl()).toBe("/api/insights");
  });

  it("keeps the issue list unwindowed even when it is filtered", async () => {
    await insightsQuery({ status: "RESOLVED" }).queryFn();
    const url = new URL(requestedUrl(), "http://x");
    expect(url.searchParams.get("status")).toBe("RESOLVED");
    expect(url.searchParams.has("windowStart")).toBe(false);
  });
});
