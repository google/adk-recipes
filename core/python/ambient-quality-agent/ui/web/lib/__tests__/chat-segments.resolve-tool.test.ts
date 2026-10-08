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

import { describe, expect, it } from "vitest";
import { resolveToolResults } from "@/lib/chat-segments";
import type { ChatMessage } from "@/components/chat/chat-shell";

function msg(id: string, segments: ChatMessage["segments"]): ChatMessage {
  return { id, role: "assistant", createdAt: 0, segments };
}

describe("resolveToolResults", () => {
  it("attaches an orphan tool_result to a spinning tool segment in another bubble", () => {
    const messages: ChatMessage[] = [
      msg("a", [
        {
          kind: "tool",
          callId: "c1",
          name: "terminal",
          args: { command: "ls" },
        },
      ]),
      msg("b", [{ kind: "text", text: "ok" }]),
    ];
    const out = resolveToolResults(messages, [
      { callId: "c1", name: "terminal", result: { stdout: "x", exit_code: 0 } },
    ]);
    const tool = out.flatMap((m) => m.segments).find((s) => s.kind === "tool");
    expect(tool).toMatchObject({
      hasResult: true,
      result: { stdout: "x", exit_code: 0 },
    });
  });

  it("is idempotent — returns the same reference when nothing changes", () => {
    const messages: ChatMessage[] = [
      msg("a", [
        {
          kind: "tool",
          callId: "c1",
          name: "terminal",
          args: null,
          hasResult: true,
          result: { stdout: "done" },
        },
      ]),
    ];
    const out = resolveToolResults(messages, [
      { callId: "c1", name: "terminal", result: { stdout: "IGNORED" } },
    ]);
    expect(out).toBe(messages);
  });

  it("never cross-attaches a null-callId result", () => {
    const messages: ChatMessage[] = [
      msg("a", [{ kind: "tool", callId: "c1", name: "terminal", args: null }]),
    ];
    const out = resolveToolResults(messages, [
      { callId: null, name: "terminal", result: { stdout: "x" } },
    ]);
    expect(out).toBe(messages);
    const tool = out.flatMap((m) => m.segments).find((s) => s.kind === "tool");
    expect(tool).not.toHaveProperty("hasResult", true);
  });

  it("matches by callId only — leaves a different tool segment untouched", () => {
    const messages: ChatMessage[] = [
      msg("a", [{ kind: "tool", callId: "c1", name: "terminal", args: null }]),
      msg("b", [{ kind: "tool", callId: "c2", name: "terminal", args: null }]),
    ];
    const out = resolveToolResults(messages, [
      { callId: "c2", name: "terminal", result: { stdout: "second" } },
    ]);
    const tools = out
      .flatMap((m) => m.segments)
      .filter((s) => s.kind === "tool") as Array<{
      callId: string | null;
      hasResult?: boolean;
    }>;
    expect(tools.find((t) => t.callId === "c1")).not.toHaveProperty(
      "hasResult",
      true,
    );
    expect(tools.find((t) => t.callId === "c2")).toMatchObject({
      hasResult: true,
    });
  });
});
