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

import { describe, it, expect } from "vitest";
import { TaskNotFoundError } from "@a2a-js/sdk/errors";
import { isResubscribeInactiveError } from "@/lib/is-resubscribe-inactive-error";

// Reconstruct the exact error the SDK's _processSseEventData throws when a
// resubscribe SSE frame carries a JSON-RPC error (see
// node_modules/@a2a-js/sdk/dist/client/index.js):
//   throw new Error(
//     `SSE event contained an error: ${err.message} (Code: ${err.code}) Data: ${...}`,
//     { cause: mapToError(response) },
//   );
function sdkSseError(code: number, message: string, cause: Error): Error {
  return new Error(
    `SSE event contained an error: ${message} (Code: ${code}) Data: {}`,
    { cause },
  );
}

describe("isResubscribeInactiveError", () => {
  it("matches the SDK throw for TaskNotFound (no active queue / not found)", () => {
    const err = sdkSseError(-32001, "Task not found", new TaskNotFoundError());
    expect(isResubscribeInactiveError(err)).toBe(true);
  });

  it("matches the SDK throw for terminal task (InvalidParams -32602)", () => {
    // Terminal-task frame: backend sends InvalidParamsError, SDK maps to a
    // generic Error cause but embeds (Code: -32602) in the message.
    const err = sdkSseError(
      -32602,
      "Task t1 is in terminal state: completed",
      new Error("invalid params"),
    );
    expect(isResubscribeInactiveError(err)).toBe(true);
  });

  it("does NOT match a genuine InternalError frame (-32603)", () => {
    const err = sdkSseError(-32603, "boom", new Error("internal"));
    expect(isResubscribeInactiveError(err)).toBe(false);
  });

  it("does NOT match a transport-style error (no JSON-RPC code, no typed cause)", () => {
    expect(isResubscribeInactiveError(new TypeError("Failed to fetch"))).toBe(
      false,
    );
  });

  it("matches by typed cause even if the message lacks a parseable code", () => {
    const err = new Error("SSE event contained an error", {
      cause: new TaskNotFoundError(),
    });
    expect(isResubscribeInactiveError(err)).toBe(true);
  });

  it("is false for null / undefined", () => {
    expect(isResubscribeInactiveError(null)).toBe(false);
    expect(isResubscribeInactiveError(undefined)).toBe(false);
  });
});
