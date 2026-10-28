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

// biome-ignore-all lint/style/noNonNullAssertion: a missing value fails the test either way; the assertion only narrows the type.

import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import { renderHook, waitFor, act } from "@testing-library/react";
import type { Task } from "@a2a-js/sdk";
import { useTaskHistory } from "@/components/chat/hooks/use-task-history";
import { _resetTaskHistoryCache } from "@/lib/task-history-cache";
import type { HorizonClient } from "@/lib/a2a-client";
import type { HorizonTaskSummary } from "@/lib/horizon-tasks";

function tp(text: string) {
  return {
    content: { $case: "text", value: text },
    metadata: undefined,
    filename: "",
    mediaType: "",
  };
}

// `turns` controls how much the in-flight task has accumulated, so a refetch
// can be observed as growth rather than by counting calls alone.
function workingTask(id: string, turns: number): Task {
  const history = [{ messageId: `${id}-u`, role: "user", parts: [tp("hi")] }];
  for (let i = 0; i < turns; i++) {
    history.push({
      messageId: `${id}-a${i}`,
      role: "agent",
      parts: [tp(`step ${i}`)],
    });
  }
  return {
    id,
    contextId: "ctx",
    status: { state: "working" },
    history,
  } as unknown as Task;
}

const WORKING: HorizonTaskSummary[] = [
  { id: "t1", status: "working", isActive: true } as HorizonTaskSummary,
];

function clientReturning(get: (id: string) => Promise<Task>) {
  return { getTask: vi.fn(get) } as unknown as HorizonClient & {
    getTask: ReturnType<typeof vi.fn>;
  };
}

describe("useTaskHistory refresh while a task is in flight", () => {
  beforeEach(() => {
    _resetTaskHistoryCache();
    vi.useFakeTimers({ shouldAdvanceTime: true });
  });
  afterEach(() => {
    vi.useRealTimers();
    vi.restoreAllMocks();
  });

  it("refetches the in-flight task when progressRef advances", async () => {
    let turns = 1;
    const client = clientReturning(async (id) => workingTask(id, turns));
    const progressRef = { current: 0 };

    const { result } = renderHook(() =>
      useTaskHistory({ contextId: "ctx", tasks: WORKING, client, progressRef }),
    );
    await waitFor(() => expect(result.current.historyMessages).not.toBeNull());
    expect(client.getTask).toHaveBeenCalledTimes(1);

    // A stream event lands, then the clock ticks past the 2s floor.
    turns = 3;
    progressRef.current++;
    await act(async () => {
      await vi.advanceTimersByTimeAsync(2500);
    });

    await waitFor(() => expect(client.getTask).toHaveBeenCalledTimes(2));
  });

  it("does not refetch on a tick when progressRef has not advanced", async () => {
    const client = clientReturning(async (id) => workingTask(id, 1));
    const progressRef = { current: 0 };

    renderHook(() =>
      useTaskHistory({ contextId: "ctx", tasks: WORKING, client, progressRef }),
    );
    await waitFor(() => expect(client.getTask).toHaveBeenCalledTimes(1));

    await act(async () => {
      await vi.advanceTimersByTimeAsync(6000);
    });
    // Under the 10s fallback arm, nothing extra yet.
    expect(client.getTask).toHaveBeenCalledTimes(1);
  });

  it("falls back to a ~10s refresh when no progress events ever arrive", async () => {
    const client = clientReturning(async (id) => workingTask(id, 1));
    const progressRef = { current: 0 };

    renderHook(() =>
      useTaskHistory({ contextId: "ctx", tasks: WORKING, client, progressRef }),
    );
    await waitFor(() => expect(client.getTask).toHaveBeenCalledTimes(1));

    await act(async () => {
      await vi.advanceTimersByTimeAsync(11000);
    });
    await waitFor(() => expect(client.getTask).toHaveBeenCalledTimes(2));
  });

  it("never starts a second fetch while one is in flight", async () => {
    let release: (t: Task) => void = () => {};
    const client = clientReturning(
      () =>
        new Promise<Task>((res) => {
          release = res;
        }),
    );
    const progressRef = { current: 0 };

    renderHook(() =>
      useTaskHistory({ contextId: "ctx", tasks: WORKING, client, progressRef }),
    );
    await waitFor(() => expect(client.getTask).toHaveBeenCalledTimes(1));

    // Many ticks and many progress bumps while the first fetch hangs.
    for (let i = 0; i < 5; i++) {
      progressRef.current++;
      await act(async () => {
        await vi.advanceTimersByTimeAsync(3000);
      });
    }
    expect(client.getTask).toHaveBeenCalledTimes(1);

    // Once it completes, the pending dirty flag lets exactly one more through.
    await act(async () => {
      release(workingTask("t1", 2));
      await vi.advanceTimersByTimeAsync(2500);
    });
    await waitFor(() => expect(client.getTask).toHaveBeenCalledTimes(2));
  });

  it("still renders a fetch slower than the tick interval (starvation case)", async () => {
    let release: (t: Task) => void = () => {};
    const client = clientReturning(
      () =>
        new Promise<Task>((res) => {
          release = res;
        }),
    );
    const progressRef = { current: 0 };

    const { result } = renderHook(() =>
      useTaskHistory({ contextId: "ctx", tasks: WORKING, client, progressRef }),
    );
    // Tick repeatedly while the only fetch is still outstanding.
    for (let i = 0; i < 4; i++) {
      progressRef.current++;
      await act(async () => {
        await vi.advanceTimersByTimeAsync(2500);
      });
    }
    await act(async () => {
      release(workingTask("t1", 4));
      await vi.advanceTimersByTimeAsync(0);
    });

    await waitFor(() => expect(result.current.historyMessages).not.toBeNull());
    expect(result.current.historyMessages!.length).toBeGreaterThan(0);
  });

  it("does not toggle historyLoading on refresh-driven refetches", async () => {
    let turns = 1;
    const client = clientReturning(async (id) => workingTask(id, turns));
    const progressRef = { current: 0 };

    const { result } = renderHook(() =>
      useTaskHistory({ contextId: "ctx", tasks: WORKING, client, progressRef }),
    );
    await waitFor(() => expect(result.current.historyMessages).not.toBeNull());
    expect(result.current.historyLoading).toBe(false);

    const seen: boolean[] = [];
    turns = 2;
    progressRef.current++;
    await act(async () => {
      seen.push(result.current.historyLoading);
      await vi.advanceTimersByTimeAsync(2500);
      seen.push(result.current.historyLoading);
    });
    await waitFor(() => expect(client.getTask).toHaveBeenCalledTimes(2));
    expect(seen).toEqual([false, false]);
    expect(result.current.historyLoading).toBe(false);
  });

  it("keeps the previous bubble when a refetch fails", async () => {
    let fail = false;
    const client = clientReturning(async (id) => {
      if (fail) throw new Error("boom");
      return workingTask(id, 2);
    });
    const progressRef = { current: 0 };

    const { result } = renderHook(() =>
      useTaskHistory({ contextId: "ctx", tasks: WORKING, client, progressRef }),
    );
    await waitFor(() => expect(result.current.historyMessages).not.toBeNull());
    const before = result.current.historyMessages!.length;
    expect(before).toBeGreaterThan(0);

    fail = true;
    progressRef.current++;
    await act(async () => {
      await vi.advanceTimersByTimeAsync(2500);
    });
    await waitFor(() => expect(client.getTask).toHaveBeenCalledTimes(2));

    // The turn must not vanish on a transient error.
    expect(result.current.historyMessages!.length).toBe(before);
  });

  it("does not let a stale, smaller task replace a richer one", async () => {
    let turns = 4;
    const client = clientReturning(async (id) => workingTask(id, turns));
    const progressRef = { current: 0 };

    const { result } = renderHook(() =>
      useTaskHistory({ contextId: "ctx", tasks: WORKING, client, progressRef }),
    );
    await waitFor(() => expect(result.current.historyMessages).not.toBeNull());
    const rich = result.current.historyMessages!.length;

    // A concurrent TaskManager write can serve an older, shorter task.
    turns = 1;
    progressRef.current++;
    await act(async () => {
      await vi.advanceTimersByTimeAsync(2500);
    });
    await waitFor(() => expect(client.getTask).toHaveBeenCalledTimes(2));

    expect(result.current.historyMessages!.length).toBe(rich);
  });

  it("stops refreshing once the task goes terminal", async () => {
    const client = clientReturning(async (id) => workingTask(id, 2));
    const progressRef = { current: 0 };
    const done: HorizonTaskSummary[] = [
      { id: "t1", status: "completed", isActive: false } as HorizonTaskSummary,
    ];

    const { rerender } = renderHook(
      ({ tasks }) =>
        useTaskHistory({ contextId: "ctx", tasks, client, progressRef }),
      { initialProps: { tasks: WORKING } },
    );
    await waitFor(() => expect(client.getTask).toHaveBeenCalledTimes(1));

    rerender({ tasks: done });
    await waitFor(() => expect(client.getTask).toHaveBeenCalledTimes(2));
    const after = client.getTask.mock.calls.length;

    progressRef.current++;
    await act(async () => {
      await vi.advanceTimersByTimeAsync(12000);
    });
    expect(client.getTask).toHaveBeenCalledTimes(after);
  });
});
