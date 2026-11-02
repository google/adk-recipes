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

/** Typed fixtures for unit tests, in the API's shapes (`./types`). Never imported by the app. */

import type {
  AppInfo,
  ProjectSummary,
  ThreadOutcome,
  ThreadState,
  ThreadStatus,
  ThreadSummary,
  ThreadTurn,
} from "./types";

function statusForState(state: ThreadState): ThreadStatus {
  if (state === "executing") return "running";
  if (state === "awaiting_permission") return "awaiting_approval";
  return "idle";
}

function outcomeForState(state: ThreadState): ThreadOutcome {
  if (state === "completed") return "finished";
  if (state === "failed") return "failed";
  if (state === "cancelled") return "cancelled";
  return "";
}

/** A thread summary in `state`, with its `status`, `outcome`, and `is_*` flags to match. */
export function threadFixture(
  state: ThreadState = "executing",
  overrides: Partial<ThreadSummary> = {},
): ThreadSummary {
  const id = overrides.thread_id ?? "thread-1";
  const status = overrides.status ?? statusForState(state);
  const outcome = overrides.outcome ?? outcomeForState(state);
  return {
    thread_id: id,
    title: "Add greet",
    goal: "Add greet()",
    project: "code/voice-agent-playground",
    state,
    status,
    outcome,
    is_running: status === "running",
    is_gate: status === "awaiting_approval",
    is_finished: status === "idle",
    is_stale: false,
    turn_count: 1,
    latest_update: "",
    result_text: "",
    summary: "",
    error: null,
    permission: null,
    links: [],
    cards: [],
    created_at: 1_000,
    updated_at: 1_000,
    ended_at: status === "idle" ? 1_010 : null,
    elapsed_seconds: 0,
    last_seq: 0,
    ...overrides,
  };
}

/** One turn in a thread. */
export function turnFixture(
  seq = 1,
  overrides: Partial<ThreadTurn> = {},
): ThreadTurn {
  return {
    seq,
    prompt: "Add greet()",
    outcome: "finished",
    answer: "Added `greet()` in `app/greet.py`.",
    summary: "Added greet().",
    error: null,
    cards: [],
    links: [],
    started_at: 1_000,
    ended_at: 1_010,
    elapsed_seconds: 10,
    events: [],
    ...overrides,
  };
}

/** A project as `GET /api/v1/projects` lists it. */
export function projectFixture(
  id: string,
  overrides: Partial<ProjectSummary> = {},
): ProjectSummary {
  return {
    id,
    kind: "project",
    title: id.split("/").pop() ?? id,
    status_line: "",
    last_activity_at: "2026-10-25T12:00:00Z",
    needs_you: 0,
    running: 0,
    tint: 200,
    ...overrides,
  };
}

/** An app as `GET /config` lists it in `apps`; not connected unless overridden. */
export function appFixture(
  name: string,
  overrides: Partial<AppInfo> = {},
): AppInfo {
  return {
    name,
    display_name: name === "github" ? "GitHub" : "Google Workspace",
    icon: name === "github" ? "github" : "google",
    connected: false,
    account_label: null,
    needs_grant: false,
    services: [],
    manage_url: null,
    ...overrides,
  };
}
