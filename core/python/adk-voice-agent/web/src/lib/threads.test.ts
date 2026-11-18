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
import { threadFixture as thread, turnFixture } from "./fixtures";
import { relativeTime } from "./format";
import {
  showsAnswer,
  stateTone,
  statusName,
  threadElapsed,
  threadTitle,
  threadTone,
  threadWhen,
  turnElapsed,
  turnSteps,
} from "./threads";
import type { ThreadEvent, ThreadState } from "./types";

describe("threadTitle", () => {
  it("uses the title", () => {
    expect(
      threadTitle({ title: "Alaska trip itinerary", goal: "plan a trip" }),
    ).toBe("Alaska trip itinerary");
  });

  it("falls back to the request for threads without one", () => {
    expect(threadTitle({ goal: "plan a trip" })).toBe("Plan a trip");
    expect(threadTitle({ title: "  ", goal: "plan a trip" })).toBe(
      "Plan a trip",
    );
  });
});

describe("status and tone", () => {
  it("names statuses in plain words", () => {
    expect(statusName("awaiting_approval")).toBe("Needs you");
    expect(statusName("running")).toBe("Running");
    expect(statusName("idle", "finished")).toBe("Done");
    expect(statusName("idle", "failed")).toBe("Couldn't finish");
    expect(statusName("idle", "cancelled")).toBe("Stopped");
  });

  it.each<[ThreadState, string]>([
    ["awaiting_permission", "attention"],
    ["executing", "running"],
    ["completed", "success"],
    ["failed", "danger"],
    ["cancelled", "muted"],
  ])("%s has the %s tone", (state, tone) => {
    expect(stateTone(state)).toBe(tone);
    expect(threadTone(thread(state))).toBe(tone);
  });
});

describe("turnSteps", () => {
  const ev = (seq: number, kind: string, message: string): ThreadEvent => ({
    seq,
    kind,
    message,
    ts: seq,
  });

  it("leaves out raw state_changed and completed events", () => {
    const events = [
      ev(1, "state_changed", "executing"),
      ev(2, "progress", "Inspecting auth module"),
      ev(3, "activity", "Edited app/auth.py"),
      ev(4, "completed", "Done"),
    ];
    expect(turnSteps(events).map((e) => e.seq)).toEqual([2, 3]);
  });
});

describe("time", () => {
  it("formats relative ages", () => {
    expect(relativeTime(1_000, 1_030)).toBe("now");
    expect(relativeTime(1_000, 1_000 + 12 * 60)).toBe("12m");
    expect(relativeTime(1_000, 1_000 + 3 * 3600)).toBe("3h");
    expect(relativeTime(1_000, 1_000 + 2 * 86400)).toBe("2d");
  });

  it("runs the clock while in flight and freezes it once finished", () => {
    expect(
      threadElapsed(thread("executing", { elapsed_seconds: 5 }), 1_090),
    ).toBe(90);
    expect(threadWhen(thread("executing", { elapsed_seconds: 5 }), 1_090)).toBe(
      "1m 30s",
    );
    const done = thread("completed", { ended_at: 1_042, updated_at: 1_050 });
    expect(threadElapsed(done, 9_999)).toBe(42);
    expect(threadWhen(done, 1_042 + 12 * 60)).toBe("12m");
    expect(
      threadElapsed(
        thread("failed", { ended_at: null, elapsed_seconds: 7 }),
        9_999,
      ),
    ).toBe(7);

    const runningTurn = turnFixture(1, {
      outcome: "running",
      started_at: 1_000,
      ended_at: null,
      elapsed_seconds: 4,
    });
    expect(turnElapsed(runningTurn, 1_030)).toBe(30);
    const finishedTurn = turnFixture(1, {
      outcome: "finished",
      started_at: 1_000,
      ended_at: 1_015,
      elapsed_seconds: 15,
    });
    expect(turnElapsed(finishedTurn, 9_999)).toBe(15);
  });
});

describe("showsAnswer", () => {
  const doc = {
    kind: "doc" as const,
    url: "https://docs.google.com/document/d/1",
    title: "Italy vs Spain",
  };

  it("shows the answer when the turn made nothing to open", () => {
    expect(showsAnswer(turnFixture(1))).toBe(true);
  });

  it("leaves the answer out when the turn made something and has a summary", () => {
    expect(showsAnswer(turnFixture(1, { links: [doc] }))).toBe(false);
  });

  it("shows the answer when there's no summary to stand in for it", () => {
    expect(showsAnswer(turnFixture(1, { links: [doc], summary: "" }))).toBe(
      true,
    );
  });
});
