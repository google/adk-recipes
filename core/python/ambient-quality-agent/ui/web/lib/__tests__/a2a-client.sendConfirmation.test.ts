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

import { describe, expect, it, vi, beforeEach } from "vitest";

const sendMessageStreamMock = vi.fn();

// 1.0 builds clients through a factory rather than `new A2AClient(card)`.
vi.mock("@a2a-js/sdk/client", () => ({
  JsonRpcTransportFactory: class {},
  ClientFactory: class {
    async createFromAgentCard() {
      return { sendMessageStream: sendMessageStreamMock };
    }
  },
}));

// Stub the agent-card fetch the createLhaClient bootstrap requires.
beforeEach(() => {
  sendMessageStreamMock.mockReset();
  global.fetch = vi.fn(
    async () =>
      new Response(
        JSON.stringify({
          name: "lha",
          supportedInterfaces: [
            {
              protocolBinding: "JSONRPC",
              protocolVersion: "1.0",
              url: "http://test/a2a",
              tenant: "",
            },
          ],
          capabilities: { extensions: [] },
        }),
        { status: 200, headers: { "content-type": "application/json" } },
      ),
  ) as unknown as typeof fetch;
});

import { createLhaClient } from "@/lib/a2a-client";
import { dataPartDict } from "@/lib/a2a-part";

async function* yieldNothing() {
  // Empty event stream — the test only inspects the OUTGOING params.
}

describe("sendConfirmation", () => {
  it("builds a continuation Message carrying an ADK function_response DataPart", async () => {
    sendMessageStreamMock.mockReturnValue(yieldNothing());

    const c = await createLhaClient({
      contextId: "ctx-test",
    });

    const stream = c.sendConfirmation({
      callId: "adk-conf-1",
      confirmed: true,
      payload: { choice: "line" },
    });
    // Drain the (empty) iterator so the body runs.
    for await (const _ of stream) {
      // no-op
    }

    expect(sendMessageStreamMock).toHaveBeenCalledTimes(1);
    const [params] = sendMessageStreamMock.mock.calls[0];
    // 1.0 Message has no `kind` discriminator and `role` is a numeric enum
    // (Role.ROLE_USER === 1).
    expect(params.message.role).toBe(1);
    expect(params.message.contextId).toBe("ctx-test");
    expect(params.message.parts).toHaveLength(1);
    const part = params.message.parts[0];
    // ADK's `_parse_tool_confirmation` expects the response dict to be the
    // ToolConfirmation fields directly — not wrapped under a `toolConfirmation`
    // key. See the matching comment in lib/a2a-client.ts.
    expect(dataPartDict(part)).toEqual({
      id: "adk-conf-1",
      name: "adk_request_confirmation",
      response: {
        confirmed: true,
        payload: { choice: "line" },
      },
    });
    // adk_type is PART metadata, not part of the data payload — the backend
    // keys off it to recognise the function response.
    expect(part.metadata).toEqual({ adk_type: "function_response" });
  });

  it("sends confirmed=false with null payload on Decline", async () => {
    sendMessageStreamMock.mockReturnValue(yieldNothing());

    const c = await createLhaClient({
      contextId: "ctx-test",
    });

    const stream = c.sendConfirmation({
      callId: "adk-conf-2",
      confirmed: false,
      payload: null,
    });
    for await (const _ of stream) {
      // no-op
    }

    const [params] = sendMessageStreamMock.mock.calls[0];
    const part = params.message.parts[0];
    expect((dataPartDict(part) as { response: unknown }).response).toEqual({
      confirmed: false,
      payload: null,
    });
  });

  it("uses the override toolName when provided", async () => {
    sendMessageStreamMock.mockReturnValue(yieldNothing());

    const c = await createLhaClient({
      contextId: "ctx-test",
    });

    const stream = c.sendConfirmation({
      callId: "adk-call-clarify-1",
      confirmed: true,
      payload: { choice: "bar" },
      toolName: "clarify",
    });
    for await (const _ of stream) {
      // no-op
    }

    const [params] = sendMessageStreamMock.mock.calls[0];
    expect(
      (dataPartDict(params.message.parts[0]) as { name: string }).name,
    ).toBe("clarify");
  });
});
