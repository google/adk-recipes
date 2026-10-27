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

const cancelTaskMock = vi.fn();

// 1.0 builds clients through a factory rather than `new A2AClient(card)`.
vi.mock("@a2a-js/sdk/client", () => ({
  JsonRpcTransportFactory: class {},
  ClientFactory: class {
    async createFromAgentCard() {
      return { cancelTask: cancelTaskMock };
    }
  },
}));

beforeEach(() => {
  cancelTaskMock.mockReset();
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

describe("cancelTask", () => {
  it("calls the underlying A2AClient.cancelTask with the task id", async () => {
    cancelTaskMock.mockResolvedValue({ id: "task-1", kind: "task" });

    const c = await createLhaClient({ contextId: "ctx-test" });
    await c.cancelTask("task-1");

    expect(cancelTaskMock).toHaveBeenCalledTimes(1);
    // 1.0 CancelTaskRequest is { tenant, id, metadata }.
    expect(cancelTaskMock.mock.calls[0][0]).toEqual({
      tenant: "",
      id: "task-1",
      metadata: undefined,
    });
  });

  it("propagates RPC errors so the caller can ignore them", async () => {
    cancelTaskMock.mockRejectedValue(new Error("Task cannot be canceled"));

    const c = await createLhaClient({ contextId: "ctx-test" });
    await expect(c.cancelTask("task-2")).rejects.toThrow(
      "Task cannot be canceled",
    );
  });
});
