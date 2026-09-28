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

import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, waitFor, act } from "@testing-library/react";
import { createElement, type ReactNode } from "react";
import { QueryClientProvider, type QueryClient } from "@tanstack/react-query";
import { makeQueryClient } from "../query-client";
import { qk } from "../query-keys";
import {
  useLhaState,
  type HorizonStateResponse,
  type HorizonState,
} from "../horizon-state";

function wrapper(client: QueryClient) {
  return ({ children }: { children: ReactNode }) =>
    createElement(QueryClientProvider, { client }, children);
}

function baseState(overrides?: Partial<HorizonState>): HorizonState {
  return {
    skill_telemetry: {},
    bound_skills: [],
    halt_reason: null,
    last_error: null,
    iteration: 1,
    tool_calls_this_iteration: 0,
    iteration_halted: false,
    iteration_halt_reason: null,
    session_started: true,
    session_started_at: null,
    pending_confirmation: null,
    pending_credential: null,
    tool_call_log: [],
    in_flight_tool_call: null,
    delegate_history: [],
    memory_writes: [],
    sandbox_status: null,
    ...overrides,
  };
}

function response(state: HorizonState): HorizonStateResponse {
  return { contextId: "c", exists: true, state };
}

beforeEach(() => {
  vi.stubGlobal(
    "fetch",
    vi.fn(
      async () =>
        new Response(JSON.stringify(response(baseState())), { status: 200 }),
    ),
  );
});

describe("useLhaState", () => {
  it("projects the selected slice and caches under qk.state(contextId)", async () => {
    const client = makeQueryClient();
    let seen: { iteration: number } | undefined;
    function Probe() {
      seen = useLhaState("c", { select: (d) => ({ iteration: d.state.iteration }) }).data;
      return createElement("span", null, JSON.stringify(seen));
    }
    render(createElement(Probe), { wrapper: wrapper(client) });
    await waitFor(() => expect(seen).toEqual({ iteration: 1 }));
    const cached = client.getQueryData<HorizonStateResponse>(qk.state("c"));
    expect(cached?.state.iteration).toBe(1);
  });

  it("does not re-render a consumer when its selected slice is value-equal across an update", async () => {
    const client = makeQueryClient();
    // Seed cache: a tool_call_log slice (array of objects) — the JSON-fallback case.
    const seedLog = [{ name: "a", args: "{}", ok: true, ts: 1 }];
    client.setQueryData(
      qk.state("c"),
      response(baseState({ tool_call_log: seedLog, iteration: 1 })),
    );

    let renders = 0;
    function Probe() {
      renders++;
      const log = useLhaState("c", {
        select: (d) => d.state.tool_call_log,
      }).data;
      return createElement("span", null, JSON.stringify(log));
    }
    render(createElement(Probe), { wrapper: wrapper(client) });
    await waitFor(() => expect(renders).toBeGreaterThan(0));
    const rendersAfterLoad = renders;

    // New full response: a fresh tool_call_log array with value-equal contents
    // (different object refs), plus an unrelated field change (iteration bump).
    await act(async () => {
      client.setQueryData(
        qk.state("c"),
        response(
          baseState({
            tool_call_log: [{ name: "a", args: "{}", ok: true, ts: 1 }],
            iteration: 2,
          }),
        ),
      );
      // Give the observer's batched notification a chance to flush so a stray
      // re-render would be observable here if structural sharing failed.
      await new Promise((r) => setTimeout(r, 0));
    });

    // The selected slice (tool_call_log) is value-equal, so the consumer must
    // NOT re-render even though the full response object and a sibling field changed.
    expect(renders).toBe(rendersAfterLoad);
  });

  it("does re-render when the selected slice actually changes value", async () => {
    const client = makeQueryClient();
    client.setQueryData(
      qk.state("c"),
      response(baseState({ tool_call_log: [{ name: "a", args: "{}", ok: true, ts: 1 }] })),
    );
    let renders = 0;
    function Probe() {
      renders++;
      useLhaState("c", { select: (d) => d.state.tool_call_log });
      return createElement("span", null, "x");
    }
    render(createElement(Probe), { wrapper: wrapper(client) });
    await waitFor(() => expect(renders).toBeGreaterThan(0));
    const before = renders;
    act(() => {
      client.setQueryData(
        qk.state("c"),
        response(
          baseState({
            tool_call_log: [
              { name: "a", args: "{}", ok: true, ts: 1 },
              { name: "b", args: "{}", ok: false, ts: 2 },
            ],
          }),
        ),
      );
    });
    // TanStack Query batches observer notifications, so the re-render lands on a
    // later tick — wait for it rather than asserting synchronously.
    await waitFor(() => expect(renders).toBeGreaterThan(before));
  });
});
