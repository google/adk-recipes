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

import { beforeEach, describe, expect, it, vi } from "vitest";

const sendMessageStreamMock = vi.fn();

vi.mock("@a2a-js/sdk/client", () => ({
  JsonRpcTransportFactory: class {},
  ClientFactory: class {
    async createFromAgentCard() {
      return { sendMessageStream: sendMessageStreamMock };
    }
  },
}));

beforeEach(() => {
  sendMessageStreamMock.mockReset();
  sendMessageStreamMock.mockImplementation(async function* () {});
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

async function drain(stream: AsyncGenerator<unknown>): Promise<void> {
  for await (const _ of stream) {
    // The test inspects only the outgoing params.
  }
}

const readSentParams = (call = 0) => sendMessageStreamMock.mock.calls[call][0];

describe("the agent a conversation is about", () => {
  it("is named on the request, for the agent, and on the message, for the transcript", async () => {
    const c = await createLhaClient({
      contextId: "ctx",
      resolveAgentKey: () => "acme-prod__support",
    });

    await drain(c.sendStream("why?"));

    expect(readSentParams().metadata).toEqual({
      agent_key: "acme-prod__support",
    });
    expect(readSentParams().message.metadata).toEqual({
      agent_key: "acme-prod__support",
    });
  });

  it("goes with confirmations too", async () => {
    const c = await createLhaClient({
      contextId: "ctx",
      resolveAgentKey: () => "acme-prod__support",
    });

    await drain(
      c.sendConfirmation({ callId: "x", confirmed: true, payload: null }),
    );
    await drain(
      c.sendConfirmations([{ callId: "y", confirmed: false, payload: null }]),
    );

    expect(readSentParams(0).metadata).toEqual({
      agent_key: "acme-prod__support",
    });
    expect(readSentParams(1).metadata).toEqual({
      agent_key: "acme-prod__support",
    });
  });

  it("is read at each send, so a default named after boot is used", async () => {
    let agentKey: string | undefined;
    const c = await createLhaClient({
      contextId: "ctx",
      resolveAgentKey: () => agentKey,
    });

    await drain(c.sendStream("first"));
    agentKey = "support";
    await drain(c.sendStream("second"));

    expect(readSentParams(0).metadata).toBeUndefined();
    expect(readSentParams(0).message.metadata).toBeUndefined();
    expect(readSentParams(1).metadata).toEqual({ agent_key: "support" });
  });
});
