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

import { describe, expect, it, vi, beforeEach, afterEach } from "vitest";

// 1.0 builds clients through a factory rather than `new A2AClient(card)`.
vi.mock("@a2a-js/sdk/client", () => ({
  JsonRpcTransportFactory: class {},
  ClientFactory: class {
    async createFromAgentCard() {
      return {};
    }
  },
}));

import { createLhaClient, fetchAgentCard } from "@/lib/a2a-client";

function cardResponse(): Response {
  return new Response(
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
  );
}

describe("createLhaClient agent-card retry", () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });
  afterEach(() => {
    vi.useRealTimers();
  });

  it("retries a failed agent-card fetch and connects on a later attempt", async () => {
    const fetchMock = vi
      .fn()
      .mockRejectedValueOnce(new Error("network"))
      .mockResolvedValueOnce(cardResponse());
    global.fetch = fetchMock as unknown as typeof fetch;

    const pending = createLhaClient({ contextId: "ctx" });
    // Advance past the inter-attempt backoff so the second attempt fires.
    await vi.advanceTimersByTimeAsync(5_000);
    const c = await pending;

    expect(c.contextId).toBe("ctx");
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it("aborts retries and rejects when the boot signal is cancelled", async () => {
    const fetchMock = vi.fn().mockRejectedValue(new Error("network"));
    global.fetch = fetchMock as unknown as typeof fetch;

    const ctrl = new AbortController();
    const pending = createLhaClient({ contextId: "ctx", signal: ctrl.signal });
    const assertion = expect(pending).rejects.toThrow();
    ctrl.abort();
    await vi.advanceTimersByTimeAsync(5_000);
    await assertion;
  });

  it("reuses the cached agent card and coalesces concurrent in-flight requests", async () => {
    const fetchMock = vi.fn().mockImplementation(async () => cardResponse());
    global.fetch = fetchMock as unknown as typeof fetch;

    const ctrl = new AbortController();
    const [first, second] = await Promise.all([
      fetchAgentCard(),
      fetchAgentCard({ signal: ctrl.signal }),
    ]);
    const third = await fetchAgentCard();

    expect(first).toBe(second);
    expect(second).toBe(third);
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("does not cache a malformed agent card missing supportedInterfaces", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(
        new Response(JSON.stringify({ name: "broken" }), { status: 200 }),
      )
      .mockResolvedValueOnce(cardResponse());
    global.fetch = fetchMock as unknown as typeof fetch;

    await fetchAgentCard();
    const valid = await fetchAgentCard();

    expect(valid.supportedInterfaces?.length).toBe(1);
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });
});
