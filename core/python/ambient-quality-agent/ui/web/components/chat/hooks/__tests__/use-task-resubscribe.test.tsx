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
import { useTaskResubscribe } from "@/components/chat/hooks/use-task-resubscribe";
import type { HorizonClient } from "@/lib/a2a-client";
import type { ChatMessage } from "@/components/chat/chat-shell";

const flush = async () => {
  for (let i = 0; i < 5; i++) await Promise.resolve();
};

function clientThatThrows(err: Error): HorizonClient {
  return {
    resubscribeTask: async function* () {
      throw err;
    },
  } as unknown as HorizonClient;
}

function hasErrorTrailer(messages: ChatMessage[]): boolean {
  return messages.some((m) =>
    m.segments.some(
      (s) => s.kind === "text" && s.text.includes("resubscribe error"),
    ),
  );
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

describe("useTaskResubscribe transient-error suppression", () => {
  beforeEach(() => vi.useFakeTimers());
  afterEach(() => vi.useRealTimers());

  it("does not surface a resubscribe error torn down within the grace window", async () => {
    const { view, getMessages } = setup(clientThatThrows(new Error("boom")));
    await flush(); // catch runs; the error append is deferred behind the grace timer
    view.unmount(); // transient flap: effect torn down before the timer fires
    vi.advanceTimersByTime(5000);
    await flush();
    expect(hasErrorTrailer(getMessages())).toBe(false);
  });

  it("surfaces a resubscribe error that persists past the grace window", async () => {
    const { getMessages } = setup(clientThatThrows(new Error("boom")));
    await flush();
    vi.advanceTimersByTime(2000); // outlive the grace window
    await flush();
    expect(hasErrorTrailer(getMessages())).toBe(true);
  });
});
