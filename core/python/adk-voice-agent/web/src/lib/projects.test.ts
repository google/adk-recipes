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
import { computerStatus, WORKSPACE_STATUS_TEXT } from "./computer";
import { projectFixture, threadFixture } from "./fixtures";
import {
  byActivity,
  drawerSections,
  folderTitle,
  groupHomeThreads,
  liveProjectThreads,
  needsYouElsewhere,
  RECENT_SHOWN,
  recentRows,
  sameTitle,
  searchProjects,
  tintStyle,
} from "./projects";

const ids = (threads: { thread_id: string }[]) =>
  threads.map((t) => t.thread_id);

describe("groupHomeThreads", () => {
  const gateOld = threadFixture("awaiting_permission", {
    thread_id: "gate-old",
    updated_at: 20,
  });
  const gateNew = threadFixture("awaiting_permission", {
    thread_id: "gate-new",
    updated_at: 50,
  });
  const runOld = threadFixture("executing", {
    thread_id: "run-old",
    updated_at: 10,
  });
  const runNew = threadFixture("executing", {
    thread_id: "run-new",
    updated_at: 30,
  });
  const done = threadFixture("completed", {
    thread_id: "done",
    ended_at: 90,
    updated_at: 90,
  });
  const stale = threadFixture("completed", {
    thread_id: "stale",
    ended_at: 95,
    is_stale: true,
  });

  it("groups threads into needsYou, running, and recent", () => {
    const groups = groupHomeThreads([
      done,
      runNew,
      gateNew,
      runOld,
      gateOld,
      stale,
    ]);
    expect(ids(groups.needsYou)).toEqual(["gate-old", "gate-new"]);
    expect(ids(groups.running)).toEqual(["run-new", "run-old"]);
    expect(ids(groups.recent)).toEqual(["done"]);
  });
});

describe("needsYouElsewhere", () => {
  const gate = (thread_id: string, project = "projects/a") =>
    threadFixture("awaiting_permission", {
      thread_id,
      project,
      updated_at: 10,
    });
  const running = (thread_id: string) =>
    threadFixture("executing", {
      thread_id,
      project: "projects/a",
    });

  it("flags what needs you off screen: other projects or other threads", () => {
    const threads = [gate("g1", "projects/a"), running("r1")];
    expect(needsYouElsewhere(threads, { project: "projects/a" })).toBe(false);
    expect(needsYouElsewhere(threads, { project: "projects/b" })).toBe(true);
    expect(needsYouElsewhere(threads, { threadId: "g1" })).toBe(false);
    expect(needsYouElsewhere(threads, { threadId: "r1" })).toBe(true);
    expect(needsYouElsewhere(threads)).toBe(false);
  });
});

describe("drawerSections", () => {
  it("puts what needs you first, then places, then recent one-offs, in order", () => {
    const trip = projectFixture("projects/trip");
    const repo = projectFixture("code/payments", { kind: "code" });
    const email = projectFixture("projects/email", { needs_you: 1 });
    const weather = projectFixture("scratch/2026-10-25-weather", {
      kind: "scratch",
    });
    const ask = projectFixture("scratch/2026-10-24-ask", {
      kind: "scratch",
      needs_you: 2,
    });
    expect(drawerSections([trip, weather, email, repo, ask])).toEqual({
      needYou: [email, ask],
      places: [trip, repo],
      recent: [weather],
    });
  });
});

describe("recentRows", () => {
  const recent = Array.from({ length: 7 }, (_, i) =>
    projectFixture(`scratch/2026-10-2${i}-ask`, { kind: "scratch" }),
  );

  it("shows the newest few until expanded", () => {
    const folded = recentRows(recent, false);
    expect(folded.shown).toEqual(recent.slice(0, RECENT_SHOWN));
    expect(folded.hidden).toBe(7 - RECENT_SHOWN);
    expect(recentRows(recent, true)).toEqual({ shown: recent, hidden: 0 });
  });

  it("hides nothing when there are only a few", () => {
    expect(recentRows(recent.slice(0, 2), false).hidden).toBe(0);
  });
});

describe("projects", () => {
  const portland = projectFixture("projects/portland-trip", {
    title: "Portland trip",
    status_line: "Long weekend for four · Nov 14–17",
    last_activity_at: "2026-10-25T10:00:00Z",
  });
  const payments = projectFixture("code/payments", {
    kind: "code",
    title: "payments",
    status_line: "Retry fix merged",
    last_activity_at: "2026-10-25T12:00:00Z",
  });
  const note = projectFixture("scratch/2026-10-20-hello-note", {
    kind: "scratch",
    title: "Hello note",
    last_activity_at: "2026-10-20T08:00:00Z",
  });

  it("orders by most recent activity", () => {
    expect(byActivity([note, portland, payments]).map((p) => p.id)).toEqual([
      "code/payments",
      "projects/portland-trip",
      "scratch/2026-10-20-hello-note",
    ]);
  });

  it("searches title, status line, and folder, every word, any case", () => {
    const all = [portland, payments, note];
    expect(searchProjects(all, "")).toBe(all);
    expect(searchProjects(all, "  ")).toBe(all);
    expect(searchProjects(all, "PORTLAND")).toEqual([portland]);
    expect(searchProjects(all, "weekend nov")).toEqual([portland]);
    expect(searchProjects(all, "code/")).toEqual([payments]);
    expect(searchProjects(all, "portland retry")).toEqual([]);
  });

  it("names a folder readably until its summary loads", () => {
    expect(folderTitle("projects/portland-trip")).toBe("Portland trip");
    expect(folderTitle("scratch/2026-10-25-hello-note")).toBe("Hello note");
    expect(folderTitle("code/voice_agent")).toBe("Voice agent");
  });

  it("hands the tint to CSS as a hue", () => {
    expect(tintStyle(212)).toEqual({ "--project-hue": "212" });
    expect(tintStyle(360)).toEqual({ "--project-hue": "0" });
  });
});

describe("liveProjectThreads", () => {
  const project = "projects/portland-trip";
  const fetched = threadFixture("executing", {
    thread_id: "plan",
    project,
    updated_at: 100,
  });

  it("merges live /state updates and new threads for the project", () => {
    const updated = threadFixture("completed", {
      thread_id: "plan",
      project,
      result_text: "Done",
      updated_at: 120,
    });
    const added = threadFixture("executing", {
      thread_id: "hotels",
      project,
      updated_at: 150,
    });
    const elsewhere = threadFixture("executing", {
      thread_id: "other",
      project: "code/payments",
      updated_at: 200,
    });
    const merged = liveProjectThreads([fetched], project, [
      updated,
      added,
      elsewhere,
    ]);
    expect(merged.map((t) => t.thread_id)).toEqual(["hotels", "plan"]);
    expect(merged[1].result_text).toBe("Done");
  });
});

describe("computerStatus", () => {
  it("is starting while a thread boots the computer, ready while one works, else unknown", () => {
    expect(
      computerStatus([
        threadFixture("executing", {
          latest_update: "Starting your computer…",
        }),
      ]),
    ).toBe("starting");
    expect(computerStatus([threadFixture("executing")])).toBe("ready");
    expect(
      computerStatus([
        threadFixture("awaiting_permission"),
        threadFixture("completed"),
      ]),
    ).toBeNull();
    expect(computerStatus([])).toBeNull();
  });

  it("prefers the server's last-known record, falling back to the threads", () => {
    const idle = [threadFixture("completed")];
    expect(computerStatus(idle, { state: "stopped" })).toBe("stopped");
    expect(computerStatus(idle, { state: "running" })).toBe("ready");
    expect(computerStatus(idle, { state: "error" })).toBe("error");
    expect(
      computerStatus([threadFixture("executing")], {
        state: "starting",
      }),
    ).toBe("starting");
    expect(computerStatus(idle, undefined)).toBeNull();
    expect(WORKSPACE_STATUS_TEXT.stopped).toBe("Asleep");
  });
});

describe("sameTitle", () => {
  it("ignores case, punctuation, and spacing", () => {
    expect(sameTitle("Europe trip planning", "europe  trip planning.")).toBe(
      true,
    );
    expect(sameTitle("Southwest National Parks trip", "Southwest trip")).toBe(
      false,
    );
  });
});
