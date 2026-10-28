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

import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import { renderHook, waitFor } from "@testing-library/react";
import { QueryClientProvider } from "@tanstack/react-query";
import { createElement, type ReactNode } from "react";
import type { Task } from "@a2a-js/sdk";
import {
  _resetTaskHistoryCache,
  getTaskHistoryCache,
} from "@/lib/task-history-cache";
import { queryClient } from "@/lib/query-client";
import { qk } from "@/lib/query-keys";
import type { HorizonClient } from "@/lib/a2a-client";
import {
  rememberTask,
  resetConversationsForTest,
} from "@/lib/aqua-conversations";
import { useLhaTasks } from "@/lib/horizon-tasks";
import { prefetchChatHistory } from "@/lib/prefetch-chat-history";

beforeEach(() => {
  _resetTaskHistoryCache();
  queryClient.clear();
  resetConversationsForTest();
  // With a jobs bucket, /lha/contexts/{id}/tasks answers the stored tasks
  // themselves under a "tasks" key, not the list useLhaTasks keeps.
  vi.stubGlobal(
    "fetch",
    vi.fn(async () =>
      Response.json({ tasks: [{ id: "t1", contextId: "ctx" }] }),
    ),
  );
});

afterEach(() => {
  vi.unstubAllGlobals();
});

function wrapper({ children }: { children: ReactNode }) {
  return createElement(QueryClientProvider, { client: queryClient }, children);
}

describe("prefetchChatHistory", () => {
  it("warms the task list useLhaTasks reads and the task-history cache", async () => {
    rememberTask("ctx", "t1");
    rememberTask("ctx", "t2");
    const getTask = vi.fn(
      async (id: string) =>
        ({ id, status: { state: "completed" } }) as unknown as Task,
    );
    const client = { getTask } as unknown as HorizonClient;

    await prefetchChatHistory("ctx", client);

    // Opening the chat right after the hover reads the warmed list.
    const { result } = renderHook(() => useLhaTasks("ctx"), { wrapper });
    await waitFor(() =>
      expect(result.current.tasks).toEqual([
        { id: "t1", status: "completed", isActive: false },
        { id: "t2", status: "completed", isActive: false },
      ]),
    );
    expect(getTask).toHaveBeenCalledTimes(2);
    const cache = getTaskHistoryCache("ctx");
    expect(cache.get("t1")).toBeDefined();
    expect(cache.get("t2")).toBeDefined();
  });

  it("also warms the tasks the server stores for the chat", async () => {
    rememberTask("ctx", "t1");
    queryClient.setQueryData(
      ["lha", "contexts"],
      [
        {
          context_id: "ctx",
          task_ids: ["t0", "t1"],
          updated_at: "2026-09-01T10:00:00Z",
        },
      ],
    );
    const getTask = vi.fn(
      async (id: string) =>
        ({ id, status: { state: "completed" } }) as unknown as Task,
    );

    await prefetchChatHistory("ctx", { getTask } as unknown as HorizonClient);

    expect(getTask.mock.calls.map(([id]) => id).sort()).toEqual(["t0", "t1"]);
  });

  it("no-ops without a client", async () => {
    await prefetchChatHistory("ctx", null);
    expect(getTaskHistoryCache("ctx").size).toBe(0);
    expect(queryClient.getQueryData(qk.tasks("ctx"))).toBeUndefined();
  });
});
