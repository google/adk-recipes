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
 * The run-level facts the table and the detail header share.
 *
 * `hasFailures` decides what the "only with failures" filter keeps on the runs
 * table; `metricsAllPassed` checks positive evidence that every evaluated trace
 * passed.
 */
import { describe, expect, it } from "vitest";

import { counters } from "../__fixtures__/counters";
import {
  ABSENT,
  agentLabel,
  formatInstant,
  hasFailures,
  isAmbient,
  isPastDue,
  metricsAllPassed,
  noCountsText,
  passRate,
  runStateWord,
  renderScheduledText,
  telemetryWindow,
  windowLength,
} from "../run-summary";

describe("formatInstant", () => {
  it("is absent for a missing timestamp", () => {
    expect(formatInstant(null)).toBe(ABSENT);
    expect(formatInstant("")).toBe(ABSENT);
  });

  it("passes an unparsable value through rather than hiding it", () => {
    expect(formatInstant("not a date")).toBe("not a date");
  });
});

describe("passRate", () => {
  const done = (c: Parameters<typeof counters>[0]) => ({
    status: "done",
    counters: counters(c),
  });

  it("is the share of judged trajectories that passed, leaving eval-service errors out", () => {
    expect(
      passRate(
        done({
          traces_eval_passed: 30,
          traces_eval_failed: 10,
          traces_eval_errored: 5,
        }),
      ),
    ).toBe(0.75);
  });

  it("is null when nothing was judged, or when the row names a state instead", () => {
    expect(passRate(done({ traces_eval_errored: 5 }))).toBeNull();
    expect(passRate(done({}))).toBeNull();
    expect(
      passRate({
        status: "failed",
        error: "boom",
        counters: counters({ traces_eval_passed: 3 }),
      }),
    ).toBeNull();
  });
});

describe("isAmbient", () => {
  it("counts scheduled sweeps, task fires and records older than the trigger", () => {
    for (const trigger_type of [
      "scheduled",
      "task_fire",
      "",
      null,
      undefined,
    ]) {
      expect(isAmbient({ trigger_type }), String(trigger_type)).toBe(true);
    }
  });

  it("leaves out manual and custom runs, which choose their own window or sample", () => {
    expect(isAmbient({ trigger_type: "manual" })).toBe(false);
    expect(isAmbient({ trigger_type: "Custom" })).toBe(false);
  });
});

describe("windowLength", () => {
  /** A run that started at `created`, over the window `[from, to]`. */
  const sweep = (from: string, to: string, created: string | null = to) => ({
    window_start: from,
    window_end: to,
    created_at: created,
  });

  it("gives the length of a window that ends when its run started", () => {
    expect(
      windowLength(sweep("2026-09-29T18:00:00Z", "2026-09-30T00:00:00Z")),
    ).toBe("6h");
    // The seconds between deriving the window and writing the record.
    expect(
      windowLength(
        sweep(
          "2026-09-29T18:00:00Z",
          "2026-09-30T00:00:00Z",
          "2026-09-30T00:00:22Z",
        ),
      ),
    ).toBe("6h");
  });

  it("rounds the length coarsely: minutes, then hours, then days", () => {
    expect(
      windowLength(sweep("2026-09-30T00:15:00Z", "2026-09-30T01:00:00Z")),
    ).toBe("45m");
    expect(
      windowLength(sweep("2026-09-29T00:00:00Z", "2026-09-30T06:00:00Z")),
    ).toBe("30h");
    expect(
      windowLength(sweep("2026-09-23T00:00:00Z", "2026-09-30T00:00:00Z")),
    ).toBe("7d");
  });

  it("gives none for a window that ends away from its run's start", () => {
    // A backfill: the window is last week, the run is today.
    expect(
      windowLength(
        sweep(
          "2026-09-20T00:00:00Z",
          "2026-09-21T00:00:00Z",
          "2026-09-30T09:00:00Z",
        ),
      ),
    ).toBeNull();
  });

  it("gives none for a window missing a bound", () => {
    expect(
      windowLength({
        window_end: "2026-09-30T00:00:00Z",
        created_at: "2026-09-30T00:00:00Z",
      }),
    ).toBeNull();
    expect(windowLength({})).toBeNull();
  });

  it("takes a record with no created_at to have started when its window ended", () => {
    expect(
      windowLength(sweep("2026-09-29T18:00:00Z", "2026-09-30T00:00:00Z", null)),
    ).toBe("6h");
  });
});

describe("telemetryWindow", () => {
  it("joins the two bounds with an en dash", () => {
    expect(
      telemetryWindow({
        window_start: "2026-09-13T00:00:00Z",
        window_end: "2026-09-14T00:00:00Z",
      }),
    ).toContain(" – ");
  });

  it("is absent only when neither bound is recorded", () => {
    expect(telemetryWindow({})).toBe(ABSENT);
  });

  it("still reports the half it knows", () => {
    const half = telemetryWindow({ window_end: "2026-09-14T00:00:00Z" });
    expect(half).not.toBe(ABSENT);
    expect(half.startsWith(ABSENT)).toBe(true);
  });
});

describe("agentLabel", () => {
  it("names an agent-less record", () => {
    expect(agentLabel({ observed_agent_name: "" })).toBe("(unnamed agent)");
    expect(agentLabel({})).toBe("(unnamed agent)");
  });
});

describe("metricsAllPassed", () => {
  it("is true for a done run whose counters show a clean sweep", () => {
    expect(
      metricsAllPassed({
        status: "done",
        counters: counters({ traces_evaluated: 3, traces_eval_passed: 3 }),
      }),
    ).toBe(true);
  });

  it("is false when anything failed or errored", () => {
    expect(
      metricsAllPassed({
        status: "done",
        counters: counters({
          traces_evaluated: 3,
          traces_eval_passed: 2,
          traces_eval_failed: 1,
        }),
      }),
    ).toBe(false);
    expect(
      metricsAllPassed({
        status: "done",
        counters: counters({
          traces_evaluated: 3,
          traces_eval_passed: 2,
          traces_eval_errored: 1,
        }),
      }),
    ).toBe(false);
  });

  it("is false for a run that evaluated nothing", () => {
    // Zero evaluated is not a pass; hiding it would be the filter claiming a
    // clean sweep it never saw.
    expect(
      metricsAllPassed({
        status: "done",
        counters: counters({ traces_scanned: 9 }),
      }),
    ).toBe(false);
  });

  it("is false for a run still in flight, however clean it looks", () => {
    expect(
      metricsAllPassed({
        status: "running",
        counters: counters({ traces_evaluated: 3, traces_eval_passed: 3 }),
      }),
    ).toBe(false);
  });

  it("is false for a done run that carries an error", () => {
    expect(
      metricsAllPassed({
        status: "done",
        error: "BigQuery said no",
        counters: counters({ traces_evaluated: 3, traces_eval_passed: 3 }),
      }),
    ).toBe(false);
  });

  it("falls back to the metric tallies for a record with no counters", () => {
    expect(
      metricsAllPassed({
        status: "done",
        metrics_passed: 4,
        metrics_failed: 0,
        metrics_errored: 0,
      }),
    ).toBe(true);
    expect(
      metricsAllPassed({
        status: "done",
        metrics_passed: 4,
        metrics_failed: 1,
        metrics_errored: 0,
      }),
    ).toBe(false);
  });

  it("is false when neither source can answer", () => {
    expect(metricsAllPassed({ status: "done" })).toBe(false);
  });
});

describe("hasFailures", () => {
  it("is false for an idle done sweep whose counters are all zero", () => {
    expect(
      hasFailures({
        status: "done",
        error: null,
        counters: counters({}),
        metrics_passed: 0,
        metrics_failed: 0,
        metrics_errored: 0,
      }),
    ).toBe(false);
  });

  it("is false for a done sweep where every evaluated trace passed", () => {
    expect(
      hasFailures({
        status: "done",
        error: null,
        counters: counters({
          traces_scanned: 5,
          traces_evaluated: 5,
          traces_eval_passed: 5,
        }),
        metrics_passed: 15,
        metrics_failed: 0,
      }),
    ).toBe(false);
  });

  it("is false when candidate clusters were all rejected by verification", () => {
    expect(
      hasFailures({
        status: "done",
        error: null,
        counters: counters({
          traces_scanned: 5,
          traces_evaluated: 5,
          traces_eval_passed: 5,
          clusters_created: 2,
          clusters_rejected: 2,
          clusters_verified: 0,
        }),
      }),
    ).toBe(false);
  });

  it("is false for duplicate-trigger skipped runs and empty-deployment no-data errors", () => {
    expect(
      hasFailures({
        status: "skipped",
        error: "duplicate trigger; skipped",
        counters: counters({}),
      }),
    ).toBe(false);
    expect(
      hasFailures({
        status: "failed",
        error:
          "NotFound: 404 Table project:dataset.spans was not found in location us-east1",
        counters: counters({}),
      }),
    ).toBe(false);
  });

  it("is true when a sweep failed, stalled, or recorded a real error", () => {
    expect(hasFailures({ status: "failed", counters: counters({}) })).toBe(
      true,
    );
    expect(
      hasFailures({
        status: "done",
        error: "BigQuery timeout",
        counters: counters({}),
      }),
    ).toBe(true);
    expect(
      hasFailures({
        status: "running",
        created_at: "2020-01-01T00:00:00Z",
        counters: counters({}),
      }),
    ).toBe(true);
  });

  it("is true when traces were scanned in the window but none survived to evaluation", () => {
    expect(
      hasFailures({
        status: "done",
        counters: counters({ traces_scanned: 9, traces_evaluated: 0 }),
      }),
    ).toBe(true);
  });

  it("is true when any trace failed evaluation, produced insights, or failed legacy metrics", () => {
    expect(
      hasFailures({
        status: "done",
        counters: counters({
          traces_evaluated: 5,
          traces_eval_passed: 2,
          traces_eval_failed: 3,
        }),
      }),
    ).toBe(true);
    expect(
      hasFailures({
        status: "done",
        counters: counters({ insights_created: 1 }),
      }),
    ).toBe(true);
    expect(
      hasFailures({
        status: "done",
        metrics_passed: 4,
        metrics_failed: 1,
        metrics_errored: 0,
      }),
    ).toBe(true);
  });
});

describe("runStateWord", () => {
  const recent = new Date().toISOString();
  const NO_DATA = "Not found: Table p:d.t was not found";

  it.each([
    [{ status: "skipped", error: "boom" }, "skipped"],
    [{ status: "done", error: "boom" }, "failed"],
    [{ status: "failed", error: NO_DATA }, "no data"],
    [{ status: "failed" }, "failed"],
    [{ status: "running", created_at: "2020-01-01T00:00:00Z" }, "stalled"],
    [{ status: "running", created_at: recent }, "running"],
    [{ status: "PENDING", created_at: recent }, "pending"],
    // Waiting for its trigger is not stalling, however long the delay is.
    [{ status: "scheduled", created_at: "2020-01-01T00:00:00Z" }, "scheduled"],
    [{ status: "done", counters: counters({}) }, "no trajectories evaluated"],
    [{ status: "done" }, "no trajectories evaluated"],
  ])("names the state of %j as %s", (run, word) => {
    expect(runStateWord(run)).toBe(word);
  });

  it("is null for a run with evaluated traces, whose counts say more", () => {
    expect(
      runStateWord({
        status: "done",
        counters: counters({ traces_eval_failed: 1 }),
      }),
    ).toBeNull();
  });
});

describe("a scheduled run", () => {
  const due = "2026-10-07T17:15:00Z";
  const run = { status: "scheduled", due_at: due };
  const before = Date.parse(due) - 60_000;
  const after = Date.parse(due) + 60_000;

  it("says when it starts and why it waits", () => {
    const text = renderScheduledText(run, before);
    expect(text).toContain(`Starts at ${formatInstant(due)}`);
    expect(text).toContain("redeployed");
  });

  it("says it is late once its due time has passed", () => {
    expect(isPastDue(run, before)).toBe(false);
    expect(isPastDue(run, after)).toBe(true);
    expect(renderScheduledText(run, after)).toContain("has not started yet");
  });

  it("is never past due without a due time, or once it has moved on", () => {
    expect(isPastDue({ status: "scheduled" }, after)).toBe(false);
    expect(isPastDue({ status: "pending", due_at: due }, after)).toBe(false);
    expect(
      renderScheduledText({ status: "pending", due_at: due }, after),
    ).toBeNull();
  });

  it("has no counts because it has not started", () => {
    expect(noCountsText(run)).toContain("has not started");
  });
});
