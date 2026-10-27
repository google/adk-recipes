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

import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { renderHook } from "@testing-library/react";
import { TaskNotFoundError } from "@a2a-js/sdk/errors";
import { useTaskResubscribe } from "@/components/chat/hooks/use-task-resubscribe";
import type { HorizonClient } from "@/lib/a2a-client";
import type { ChatMessage } from "@/components/chat/chat-shell";

const flush = async () => {
  for (let i = 0; i < 8; i++) await Promise.resolve();
};

// Reproduce EXACTLY what @a2a-js/sdk's _processSseEventData does when a
// resubscribe SSE frame carries a JSON-RPC error: it THROWS (it never yields
// the error object). See node_modules/@a2a-js/sdk/dist/client/index.js:
//   throw new Error(
//     `SSE event contained an error: ${err.message} (Code: ${err.code}) ...`,
//     { cause: mapToError(response) },
//   );
function sdkResubscribeThrow(
  code: number,
  message: string,
  cause: Error,
): Error {
  return new Error(
    `SSE event contained an error: ${message} (Code: ${code}) Data: {}`,
    { cause },
  );
}

// Returns a stream whose first read rejects with `err`, matching the SDK when
// the server refuses a resubscribe.
function buildRejectingStream(err: Error): AsyncIterable<never> {
  return {
    [Symbol.asyncIterator]: () => ({ next: () => Promise.reject(err) }),
  };
}

// Client whose resubscribe stream THROWS the SDK-shaped "task not active here"
// error on first iteration (the real SDK behavior), and whose getTask returns a
// completed task with text.
function clientThrowsNotActive(): HorizonClient {
  return {
    resubscribeTask: () =>
      buildRejectingStream(
        sdkResubscribeThrow(-32001, "Task not found", new TaskNotFoundError()),
      ),
    getTask: async () => ({
      id: "t1",
      status: { state: "completed" },
      history: [
        {
          role: "agent",
          parts: [
            {
              content: { $case: "text", value: "done!" },
              metadata: undefined,
              filename: "",
              mediaType: "",
            },
          ],
        },
      ],
    }),
  } as unknown as HorizonClient;
}

// Client whose resubscribe THROWS a genuine InternalError frame — NOT
// recoverable; the hook must render the resubscribe-error trailer (after the
// grace window), not call getTask.
function clientThrowsInternal(
  getTask: ReturnType<typeof vi.fn>,
): HorizonClient {
  return {
    resubscribeTask: () =>
      buildRejectingStream(
        sdkResubscribeThrow(-32603, "boom", new Error("internal")),
      ),
    getTask,
  } as unknown as HorizonClient;
}

function setup(client: HorizonClient) {
  let messages: ChatMessage[] = [];
  const setMessages = (u: React.SetStateAction<ChatMessage[]>) => {
    messages =
      typeof u === "function"
        ? (u as (p: ChatMessage[]) => ChatMessage[])(messages)
        : u;
  };
  const view = renderHook(() =>
    useTaskResubscribe({
      activeTaskId: "t1",
      client,
      suspended: false,
      setMessages,
      onTerminal: vi.fn(async () => {}),
    }),
  );
  return { view, getMessages: () => messages };
}

function joinedText(messages: ChatMessage[]): string {
  return messages
    .flatMap((m) => m.segments)
    .filter((s) => s.kind === "text")
    .map((s) => (s as { text: string }).text)
    .join("");
}

describe("useTaskResubscribe getTask fallback (real SDK throw path)", () => {
  beforeEach(() => vi.useFakeTimers());
  afterEach(() => vi.useRealTimers());

  it("renders the finished task via getTask when resubscribe THROWS not-active", async () => {
    const { getMessages } = setup(clientThrowsNotActive());
    await flush();
    expect(joinedText(getMessages())).toContain("done!");
  });

  it("does NOT call getTask and surfaces the trailer for a genuine InternalError throw", async () => {
    const getTask = vi.fn();
    const { getMessages } = setup(clientThrowsInternal(getTask));
    await flush();
    expect(getTask).not.toHaveBeenCalled();
    // Trailer is deferred behind the grace window; advance past it.
    await vi.advanceTimersByTimeAsync(2000);
    expect(joinedText(getMessages())).toContain("resubscribe error");
  });
});
