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
import { rememberTask, resetConversationsForTest } from "../aqua-conversations";

function wrapper(client = makeQueryClient()) {
  return ({ children }: { children: ReactNode }) =>
    createElement(QueryClientProvider, { client }, children);
}

// The browser's conversation store records each turn's task; the tasks the
// server stores are added where /lha/contexts lists them.
beforeEach(() => {
  resetConversationsForTest();
  rememberTask("ctx-1", "t-done");
  rememberTask("ctx-1", "t-live");
  vi.stubGlobal(
    "fetch",
    vi.fn(async () => new Response("{}", { status: 200 })),
  );
});

describe("useLhaTasks", () => {
  it("loads tasks and derives activeTaskId from the active task", async () => {
    const { result } = renderHook(() => useLhaTasks("ctx-1"), {
      wrapper: wrapper(),
    });
    await waitFor(() => expect(result.current.tasks).toHaveLength(2));
    // Replayed tasks are all finished, so nothing is active: a live turn
    // streams rather than being replayed.
    expect(result.current.tasks?.map((t) => t.id)).toEqual([
      "t-done",
      "t-live",
    ]);
    expect(result.current.activeTaskId).toBeNull();
  });

  it("populates the TanStack Query cache under qk.tasks(contextId)", async () => {
    const client = makeQueryClient();
    const { result } = renderHook(() => useLhaTasks("ctx-1"), {
      wrapper: wrapper(client),
    });
    await waitFor(() => expect(result.current.isLoading).toBe(false));
    const cached = client.getQueryData<HorizonTaskSummary[]>(qk.tasks("ctx-1"));
    expect(cached).toHaveLength(2);
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

  it("refresh() picks up a task recorded after the first read", async () => {
    const { result } = renderHook(() => useLhaTasks("ctx-1"), {
      wrapper: wrapper(),
    });
    await waitFor(() => expect(result.current.tasks).toHaveLength(2));
    rememberTask("ctx-1", "t-third");
    await result.current.refresh();
    await waitFor(() => expect(result.current.tasks).toHaveLength(3));
  });
});

function stubContexts(answer: unknown[] | "pending" | 501) {
  vi.stubGlobal(
    "fetch",
    vi.fn((url: string) => {
      if (!url.startsWith("/lha/contexts")) {
        return Promise.resolve(new Response("{}", { status: 200 }));
      }
      if (answer === "pending") return new Promise(() => {});
      if (answer === 501) {
        return Promise.resolve(new Response("{}", { status: 501 }));
      }
      return Promise.resolve(Response.json({ contexts: answer }));
    }),
  );
}

function buildStoredContext(contextId: string, taskIds: string[]) {
  return {
    context_id: contextId,
    task_ids: taskIds,
    updated_at: "2026-09-01T10:00:00Z",
  };
}

describe("useLhaTasks with the tasks the server stores", () => {
  it("lists the stored tasks of a conversation this browser never recorded", async () => {
    stubContexts([buildStoredContext("ctx-remote", ["t-r1", "t-r2"])]);
    const { result } = renderHook(() => useLhaTasks("ctx-remote"), {
      wrapper: wrapper(),
    });
    await waitFor(() =>
      expect(result.current.tasks?.map((t) => t.id)).toEqual(["t-r1", "t-r2"]),
    );
  });

  it("adds a stored task this browser missed, in the server's order", async () => {
    // A stream that ended before it named its task leaves the task stored
    // and its id unrecorded here.
    stubContexts([
      buildStoredContext("ctx-1", ["t-first", "t-done", "t-live"]),
    ]);
    const { result } = renderHook(() => useLhaTasks("ctx-1"), {
      wrapper: wrapper(),
    });
    await waitFor(() =>
      expect(result.current.tasks?.map((t) => t.id)).toEqual([
        "t-first",
        "t-done",
        "t-live",
      ]),
    );
  });

  it("puts a task the server has not stored yet after the stored ones", async () => {
    rememberTask("ctx-1", "t-new");
    stubContexts([
      buildStoredContext("ctx-1", ["t-first", "t-done", "t-live"]),
    ]);
    const { result } = renderHook(() => useLhaTasks("ctx-1"), {
      wrapper: wrapper(),
    });
    await waitFor(() =>
      expect(result.current.tasks?.map((t) => t.id)).toEqual([
        "t-first",
        "t-done",
        "t-live",
        "t-new",
      ]),
    );
  });

  it("keeps this browser's order when it recorded every stored task", async () => {
    stubContexts([buildStoredContext("ctx-1", ["t-live", "t-done"])]);
    const client = makeQueryClient();
    const { result } = renderHook(() => useLhaTasks("ctx-1"), {
      wrapper: wrapper(client),
    });
    await waitFor(() =>
      expect(client.getQueryData(["lha", "contexts"])).toBeDefined(),
    );
    expect(result.current.tasks?.map((t) => t.id)).toEqual([
      "t-done",
      "t-live",
    ]);
  });

  it("does not call an unrecorded conversation empty while the server's list loads", async () => {
    stubContexts("pending");
    const { result } = renderHook(() => useLhaTasks("ctx-remote"), {
      wrapper: wrapper(),
    });
    await new Promise((r) => setTimeout(r, 50));
    expect(result.current.tasks).toBeUndefined();
  });

  it("uses this browser's tasks when the deployment stores none", async () => {
    stubContexts(501);
    const { result } = renderHook(() => useLhaTasks("ctx-1"), {
      wrapper: wrapper(),
    });
    await waitFor(() =>
      expect(result.current.tasks?.map((t) => t.id)).toEqual([
        "t-done",
        "t-live",
      ]),
    );
  });
});
