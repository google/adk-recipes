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
import { findMatches } from "@/lib/find-in-conversation";
import type { ChatMessage } from "@/components/chat/chat-shell";

function m(id: string, text: string): ChatMessage {
  return {
    id,
    role: "assistant",
    segments: [{ kind: "text", text }],
    createdAt: 0,
  };
}

describe("findMatches", () => {
  it("returns nothing for an empty query", () => {
    expect(findMatches([m("a", "hello world")], "")).toEqual([]);
    expect(findMatches([m("a", "hello")], "   ")).toEqual([]);
  });

  it("matches text segments case-insensitively and preserves order", () => {
    const msgs = [
      m("a", "The Quick Brown Fox"),
      m("b", "lazy dog"),
      m("c", "FOX hunt"),
    ];
    expect(findMatches(msgs, "fox")).toEqual(["a", "c"]);
  });

  it("also searches tool names", () => {
    const msgs: ChatMessage[] = [
      {
        id: "t",
        role: "assistant",
        segments: [
          { kind: "tool", callId: "1", name: "web_search", args: null },
        ],
        createdAt: 0,
      },
    ];
    expect(findMatches(msgs, "web_search")).toEqual(["t"]);
  });

  it("returns an empty list when nothing matches", () => {
    expect(findMatches([m("a", "alpha"), m("b", "beta")], "zzz")).toEqual([]);
  });
});
