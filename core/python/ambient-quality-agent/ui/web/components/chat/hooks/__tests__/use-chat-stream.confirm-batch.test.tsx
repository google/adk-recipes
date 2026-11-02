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

import { describe, it, expect, vi } from "vitest";
import { renderHook, act, waitFor } from "@testing-library/react";
import { useChatStream } from "@/components/chat/hooks/use-chat-stream";
import type { HorizonClient } from "@/lib/a2a-client";

async function* emptyStream() {
  // no events; a batched approve continuation that yields nothing observable
}

function makeClient() {
  const sendConfirmations = vi.fn(() => emptyStream());
  const client = {
    contextId: "ctx",
    sendConfirmations,
  } as unknown as HorizonClient;
  return { client, sendConfirmations };
}

function permSeg(callId: string) {
  return {
    kind: "confirmation" as const,
    callId,
    hint: `Run: cmd ${callId}`,
    originalCall: { name: "terminal", args: null },
    payload: {
      kind: "permission",
      toolName: "terminal",
      summary: `Run: cmd ${callId}`,
      proposedRule: {},
      choices: [],
    },
  };
}

describe("useChatStream confirm() batch", () => {
  it("flips all cards to resolved and opens a single batched stream", async () => {
    const { client, sendConfirmations } = makeClient();
    const { result } = renderHook(() =>
      useChatStream({ client, contextIdProp: "ctx" }),
    );

    act(() => {
      result.current.setMessages([
        {
          id: "prev",
          role: "assistant",
          createdAt: 1,
          segments: [permSeg("c1"), permSeg("c2")],
        },
      ]);
    });

    await act(async () => {
      await result.current.confirm([
        {
          callId: "c1",
          confirmed: true,
          payload: { outcome: "proceed_always" },
        },
        {
          callId: "c2",
          confirmed: true,
          payload: { outcome: "proceed_always" },
        },
      ]);
    });

    expect(sendConfirmations).toHaveBeenCalledTimes(1);
    expect(sendConfirmations).toHaveBeenCalledWith(
      [
        {
          callId: "c1",
          confirmed: true,
          payload: { outcome: "proceed_always" },
        },
        {
          callId: "c2",
          confirmed: true,
          payload: { outcome: "proceed_always" },
        },
      ],
      { signal: expect.any(AbortSignal) },
    );

    await waitFor(() => {
      const confirmations = result.current.messages
        .flatMap((m) => m.segments)
        .filter((s) => s.kind === "confirmation");
      expect(confirmations.every((s) => s.answer !== undefined)).toBe(true);
    });
  });
});
