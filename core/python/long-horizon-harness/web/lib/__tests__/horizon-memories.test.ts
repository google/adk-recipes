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
import { useLhaMemories, type HorizonMemoriesResponse } from "../horizon-memories";

function wrapper(client = makeQueryClient()) {
  return ({ children }: { children: ReactNode }) =>
    createElement(QueryClientProvider, { client }, children);
}

function fixture(): HorizonMemoriesResponse {
  return {
    entries: [
      { scope: "user", content: "prefers espresso", ts_ms: 1, session_id: "s1" },
    ],
    counts: { user: 1, agent: 0, user_profile: 0 },
    has_more: false,
  };
}

beforeEach(() => {
  vi.stubGlobal(
    "fetch",
    vi.fn(
      async () => new Response(JSON.stringify(fixture()), { status: 200 }),
    ),
  );
});

describe("useLhaMemories", () => {
  it("loads memories", async () => {
    const { result } = renderHook(() => useLhaMemories(5), {
      wrapper: wrapper(),
    });
    await waitFor(() => expect(result.current.data?.entries).toHaveLength(1));
    expect(result.current.data?.entries[0]?.content).toBe("prefers espresso");
  });

  it("populates the TanStack Query cache under qk.memories(limit)", async () => {
    const client = makeQueryClient();
    const { result } = renderHook(() => useLhaMemories(5), {
      wrapper: wrapper(client),
    });
    await waitFor(() => expect(result.current.isLoading).toBe(false));
    const cached = client.getQueryData<HorizonMemoriesResponse>(qk.memories(5));
    expect(cached?.entries).toHaveLength(1);
  });

  it("refresh() invalidates the query and refetches", async () => {
    const { result } = renderHook(() => useLhaMemories(5), {
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
