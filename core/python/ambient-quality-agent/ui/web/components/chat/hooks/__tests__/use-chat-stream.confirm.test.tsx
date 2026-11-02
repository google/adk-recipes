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

// A confirmation continuation stream: one statusUpdate (1.0 StreamResponse)
// carrying the resumed terminal function_response for callId "c1".
async function* continuationStream() {
  yield {
    payload: {
      $case: "statusUpdate",
      value: {
        taskId: "t2",
        contextId: "ctx",
        status: {
          state: 3, // TASK_STATE_COMPLETED
          message: {
            role: 2, // ROLE_AGENT
            parts: [
              {
                content: {
                  $case: "data",
                  value: {
                    id: "c1",
                    name: "terminal",
                    response: { stdout: "done", exit_code: 0 },
                  },
                },
                metadata: { adk_type: "function_response" },
                filename: "",
                mediaType: "",
              },
            ],
          },
          timestamp: null,
        },
        metadata: null,
      },
    },
  };
}

function makeClient(): HorizonClient {
  return {
    contextId: "ctx",
    sendConfirmation: vi.fn(() => continuationStream()),
  } as unknown as HorizonClient;
}

describe("useChatStream confirm() clears the gated tool spinner", () => {
  it("attaches the resumed tool result to the original spinning segment", async () => {
    const client = makeClient();
    const { result } = renderHook(() =>
      useChatStream({
        client,
        contextIdProp: "ctx",
      }),
    );

    // Seed a prior assistant bubble holding the spinning terminal segment.
    act(() => {
      result.current.setMessages([
        {
          id: "prev",
          role: "assistant",
          createdAt: 1,
          segments: [
            {
              kind: "tool",
              callId: "c1",
              name: "terminal",
              args: { command: "rm -rf x" },
            },
            {
              kind: "confirmation",
              callId: "c1-conf",
              hint: "ok?",
              originalCall: { name: "terminal", args: null },
              payload: null,
            },
          ],
        },
      ]);
    });

    await act(async () => {
      await result.current.confirm({
        callId: "c1-conf",
        confirmed: true,
        payload: null,
      });
    });

    await waitFor(() => {
      const tool = result.current.messages
        .flatMap((m) => m.segments)
        .find((s) => s.kind === "tool" && s.name === "terminal");
      expect(tool).toMatchObject({
        hasResult: true,
        result: { stdout: "done", exit_code: 0 },
      });
    });
  });
});
