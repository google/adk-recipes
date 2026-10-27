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

import { resolveHitlSegments } from "@/lib/chat-segments";
import type { ChatMessage } from "@/components/chat/chat-shell";

function makeMessages(): ChatMessage[] {
  return [
    {
      id: "m1",
      role: "assistant",
      createdAt: 1,
      segments: [
        { kind: "text", text: "hi" },
        {
          kind: "confirmation",
          callId: "c1",
          hint: "Do the thing?",
          originalCall: { name: "terminal", args: null },
          payload: { extra: 1 },
        },
      ],
    },
  ];
}

describe("resolveHitlSegments", () => {
  it("marks a matching confirmation segment answered (approve, no text)", () => {
    const next = resolveHitlSegments(makeMessages(), {
      callId: "c1",
      confirmed: true,
      payload: null,
    });
    const seg = next[0].segments[1];
    expect(seg.kind).toBe("confirmation");
    if (seg.kind === "confirmation") {
      expect(seg.answer).toEqual({ confirmed: true, text: null });
    }
  });

  it("marks a declined confirmation answered with confirmed=false", () => {
    const next = resolveHitlSegments(makeMessages(), {
      callId: "c1",
      confirmed: false,
      payload: { extra: 1 },
    });
    const seg = next[0].segments[1];
    if (seg.kind === "confirmation") {
      expect(seg.answer).toEqual({ confirmed: false, text: null });
    }
  });

  it("derives text from payload.choice for a clarify choice", () => {
    const messages: ChatMessage[] = [
      {
        id: "m1",
        role: "assistant",
        createdAt: 1,
        segments: [
          {
            kind: "clarify",
            callId: "k1",
            question: "Pick",
            choices: ["a", "b"],
          },
        ],
      },
    ];
    const next = resolveHitlSegments(messages, {
      callId: "k1",
      confirmed: true,
      payload: { choice: "b" },
    });
    const seg = next[0].segments[0];
    if (seg.kind === "clarify") {
      expect(seg.answer).toEqual({ confirmed: true, text: "b" });
    }
  });

  it("derives text from payload.answer for a clarify free-text", () => {
    const messages: ChatMessage[] = [
      {
        id: "m1",
        role: "assistant",
        createdAt: 1,
        segments: [
          { kind: "clarify", callId: "k2", question: "URL?", choices: [] },
        ],
      },
    ];
    const next = resolveHitlSegments(messages, {
      callId: "k2",
      confirmed: true,
      payload: { answer: "hi" },
    });
    const seg = next[0].segments[0];
    if (seg.kind === "clarify") {
      expect(seg.answer).toEqual({ confirmed: true, text: "hi" });
    }
  });

  it("returns the same array reference when nothing matches", () => {
    const messages = makeMessages();
    const next = resolveHitlSegments(messages, {
      callId: "nope",
      confirmed: true,
      payload: null,
    });
    expect(next).toBe(messages);
  });

  it("does not re-resolve an already-answered segment", () => {
    const messages = makeMessages();
    const first = resolveHitlSegments(messages, {
      callId: "c1",
      confirmed: true,
      payload: null,
    });
    const second = resolveHitlSegments(first, {
      callId: "c1",
      confirmed: false,
      payload: null,
    });
    const seg = second[0].segments[1];
    if (seg.kind === "confirmation") {
      // stays the first answer; the second (idempotent) call is a no-op
      expect(seg.answer).toEqual({ confirmed: true, text: null });
    }
    expect(second).toBe(first);
  });

  it("never matches a null callId", () => {
    const messages: ChatMessage[] = [
      {
        id: "m1",
        role: "assistant",
        createdAt: 1,
        segments: [
          { kind: "clarify", callId: null, question: "Pick", choices: ["a"] },
        ],
      },
    ];
    const next = resolveHitlSegments(messages, {
      callId: null,
      confirmed: true,
      payload: { choice: "a" },
    });
    expect(next).toBe(messages);
  });
});
