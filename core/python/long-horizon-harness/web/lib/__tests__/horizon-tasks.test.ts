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
import { renderHook, waitFor } from "@testing-library/react";
import { createElement, type ReactNode } from "react";
import { QueryClientProvider } from "@tanstack/react-query";
import { makeQueryClient } from "../query-client";
import { qk } from "../query-keys";
import { useLhaTasks, type HorizonTaskSummary } from "../horizon-tasks";

function wrapper(client = makeQueryClient()) {
  return ({ children }: { children: ReactNode }) =>
    createElement(QueryClientProvider, { client }, children);
}

const TASKS: HorizonTaskSummary[] = [
  { id: "t-done", status: "completed", isActive: false },
  { id: "t-live", status: "working", isActive: true },
];

beforeEach(() => {
  vi.stubGlobal(
    "fetch",
    vi.fn(
      async () => new Response(JSON.stringify(TASKS), { status: 200 }),
    ),
  );
});

describe("useLhaTasks", () => {
  it("loads tasks and derives activeTaskId from the active task", async () => {
    const { result } = renderHook(() => useLhaTasks("ctx-1"), {
      wrapper: wrapper(),
    });
    await waitFor(() => expect(result.current.tasks).toHaveLength(2));
    expect(result.current.activeTaskId).toBe("t-live");
  });

  it("populates the TanStack Query cache under qk.tasks(contextId)", async () => {
    const client = makeQueryClient();
    const { result } = renderHook(() => useLhaTasks("ctx-1"), {
      wrapper: wrapper(client),
    });
    await waitFor(() => expect(result.current.isLoading).toBe(false));
    const cached = client.getQueryData<HorizonTaskSummary[]>(qk.tasks("ctx-1"));
    expect(cached).toHaveLength(2);
    expect(cached?.find((t) => t.isActive)?.id).toBe("t-live");
  });

  it("is disabled when contextId is falsy (no fetch)", async () => {
    const { result } = renderHook(() => useLhaTasks(null), {
      wrapper: wrapper(),
    });
    // No fetch fired, query disabled.
    expect(
      (fetch as unknown as { mock: { calls: unknown[] } }).mock.calls.length,
    ).toBe(0);
    expect(result.current.tasks).toBeUndefined();
    expect(result.current.activeTaskId).toBeNull();
    expect(result.current.isLoading).toBe(false);
  });

  it("keeps a stable refresh reference across re-renders", async () => {
    // Regression: an unstable refresh identity (a fresh closure each render) fed
    // useTaskResubscribe's effect deps and drove an infinite resubscribe loop
    // that aborted the live message stream. refresh must be referentially stable.
    const { result, rerender } = renderHook(() => useLhaTasks("ctx-1"), {
      wrapper: wrapper(),
    });
    await waitFor(() => expect(result.current.isLoading).toBe(false));
    const first = result.current.refresh;
    rerender();
    rerender();
    expect(result.current.refresh).toBe(first);
  });

  it("refresh() invalidates the query and refetches", async () => {
    const { result } = renderHook(() => useLhaTasks("ctx-1"), {
      wrapper: wrapper(),
    });
    await waitFor(() => expect(result.current.isLoading).toBe(false));
    const calls = (fetch as unknown as { mock: { calls: unknown[] } }).mock.calls
      .length;
    await result.current.refresh();
    await waitFor(() =>
      expect(
        (fetch as unknown as { mock: { calls: unknown[] } }).mock.calls.length,
      ).toBeGreaterThan(calls),
    );
  });
});
