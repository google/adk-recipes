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

// The Run investigation control's one judgement.
//
// Both directions cost something real, which is why this is pinned rather than
// left to the eye: a run wrongly read as settled lets a second sweep start and
// bills for it, and a run wrongly read as in flight disables the only control
// that starts one.

import { describe, expect, test } from "vitest";
import {
  POLL_INTERVAL_MS,
  STALE_AFTER_MS,
  inFlightAge,
  inFlightRuns,
  isInFlight,
  isStalled,
  newestFirst,
  newestRun,
  runButtonLabel,
  stalledRuns,
  type RunLike,
} from "@/lib/runstate";

const NOW = Date.parse("2026-08-28T12:00:00Z");

function run(status: string, minutesAgo: number): RunLike {
  return {
    run_id: `${status}-${minutesAgo}`,
    status,
    created_at: new Date(NOW - minutesAgo * 60000).toISOString(),
  };
}

describe("what blocks the button", () => {
  test("pending and running block, done and failed do not", () => {
    expect(isInFlight(run("pending", 1), NOW)).toBe(true);
    expect(isInFlight(run("running", 1), NOW)).toBe(true);
    expect(isInFlight(run("done", 1), NOW)).toBe(false);
    expect(isInFlight(run("failed", 1), NOW)).toBe(false);
  });

  test("the status vocabulary is matched case-insensitively", () => {
    // `format_run` lowercases, but the store's enum is upper-case and the
    // record has reached the UI both ways.
    expect(isInFlight(run("PENDING", 1), NOW)).toBe(true);
    expect(isInFlight(run("Running", 1), NOW)).toBe(true);
  });

  test("an unknown or missing status is treated as settled", () => {
    // The status pill falls back to "pending" for anything it does not know,
    // which is fine for a colour and would be a lockout here. `skipped` is a
    // real one it does not know: a sweep that found nothing to do is over.
    expect(isInFlight(run("skipped", 1), NOW)).toBe(false);
    expect(isInFlight(run("cancelled", 1), NOW)).toBe(false);
    expect(isInFlight({ run_id: "x" }, NOW)).toBe(false);
    expect(isInFlight(null, NOW)).toBe(false);
  });
});

describe("when we stop believing a record", () => {
  test("a run pending for longer than the stale window stops blocking", () => {
    expect(isInFlight(run("pending", 29), NOW)).toBe(true);
    expect(isInFlight(run("pending", 31), NOW)).toBe(false);
    // The boundary itself still blocks: only *past* the window is it abandoned.
    expect(isInFlight(run("pending", STALE_AFTER_MS / 60000), NOW)).toBe(true);
  });

  test("a run with no usable timestamp blocks regardless of age", () => {
    expect(isInFlight({ status: "pending" }, NOW)).toBe(true);
    expect(
      isInFlight({ status: "running", created_at: "not a date" }, NOW),
    ).toBe(true);
  });

  test("window_end stands in for a record with no created_at", () => {
    const legacy = {
      status: "pending",
      window_end: new Date(NOW - 60000).toISOString(),
    };
    const old = {
      status: "pending",
      window_end: new Date(NOW - 3600000).toISOString(),
    };
    expect(isInFlight(legacy, NOW)).toBe(true);
    expect(isInFlight(old, NOW)).toBe(false);
  });

  test("a clock behind the server does not expire a fresh run", () => {
    // The record's timestamps are the server's UTC; a client clock a few
    // minutes behind makes a just-created run look like it starts in the
    // future.
    const future = {
      status: "pending",
      created_at: new Date(NOW + 120000).toISOString(),
    };
    expect(isInFlight(future, NOW)).toBe(true);
  });
});

describe("over a list", () => {
  test("inFlightRuns counts only the unsettled ones", () => {
    const runs = [
      run("done", 5),
      run("pending", 2),
      run("failed", 90),
      run("running", 1),
    ];
    expect(inFlightRuns(runs, NOW).map((r) => r.status)).toEqual([
      "pending",
      "running",
    ]);
    expect(inFlightRuns([], NOW)).toEqual([]);
    expect(inFlightRuns(null, NOW)).toEqual([]);
  });

  test("a written-off run is stalled, and only an unsettled one can be", () => {
    // The pair has to partition the unsettled runs: one of them explains the
    // disabled button, the other explains why it is *not* disabled, and a run
    // that fell into neither would leave the free button beside a "running" row
    // with nothing saying why -- the thing this exists to prevent.
    const wedged = run("running", 90);
    expect(isInFlight(wedged, NOW)).toBe(false);
    expect(isStalled(wedged, NOW)).toBe(true);

    const live = run("pending", 2);
    expect(isInFlight(live, NOW)).toBe(true);
    expect(isStalled(live, NOW)).toBe(false);

    expect(isStalled(run("done", 900), NOW)).toBe(false);
    expect(isStalled(run("failed", 900), NOW)).toBe(false);
    expect(
      stalledRuns([run("done", 1), wedged, live], NOW).map((r) => r.run_id),
    ).toEqual([wedged.run_id]);
  });

  test("the newest of a set is the one the note reports on", () => {
    const older = run("running", 300);
    const newer = run("running", 90);
    expect(newestRun([older, newer])?.run_id).toBe(newer.run_id);
    expect(newestRun([newer, older])?.run_id).toBe(newer.run_id);
    expect(newestRun([])).toBeNull();
  });

  test("newestFirst sorts by time, not text, and puts the undated last, in the order given", () => {
    // "+00:00" sorts after "Z" as text, which would put 06:00 above 07:00.
    const runs: RunLike[] = [
      { run_id: "undated" },
      { run_id: "six", created_at: "2026-09-30T06:00:00+00:00" },
      { run_id: "unparsable", created_at: "not a date" },
      { run_id: "seven", created_at: "2026-09-30T07:00:00Z" },
      { run_id: "legacy", window_end: "2026-09-30T08:00:00Z" },
    ];
    expect(newestFirst(runs).map((r) => r.run_id)).toEqual([
      "legacy",
      "seven",
      "six",
      "undated",
      "unparsable",
    ]);
    expect(runs[0].run_id).toBe("undated");
  });
});

describe("what it says", () => {
  test("the age of an unfinished run is coarse", () => {
    expect(inFlightAge(run("running", 0), NOW)).toBe("under a minute");
    expect(inFlightAge(run("running", 45), NOW)).toBe("45m");
    expect(inFlightAge(run("running", 60), NOW)).toBe("1h");
    expect(inFlightAge(run("running", 6 * 60), NOW)).toBe("6h");
    expect(inFlightAge(run("running", 3 * 24 * 60), NOW)).toBe("3d");
    expect(inFlightAge({ status: "running" }, NOW)).toBe("");
  });

  test("the label names how many are running", () => {
    expect(runButtonLabel(0)).toBe("Run investigation");
    expect(runButtonLabel(1)).toBe("Running…");
    expect(runButtonLabel(3)).toBe("3 running…");
  });

  test("the poll interval is the 30s the button's fill animates over", () => {
    // The fill's duration is set from this constant rather than restated in
    // CSS, so the bar cannot lie about when the list is next read.
    expect(POLL_INTERVAL_MS).toBe(30000);
  });
});
