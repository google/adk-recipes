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
import { timelineOf } from "../conversation";
import type {
  CaseConversation,
  ConversationEvent,
  ConversationPart,
  ConversationTurn,
} from "../aqua-api";

function event(
  author: string,
  ...parts: ConversationPart[]
): ConversationEvent {
  return {
    author,
    content: { role: author === "user" ? "user" : "model", parts },
  };
}

function conversation(...turns: ConversationTurn[]): CaseConversation {
  return {
    trajectory_id: "case-1",
    status: "ingested",
    turn_count: turns.length,
    turns_returned: turns.length,
    turns,
  };
}

describe("timelineOf", () => {
  it("keeps turns in the order the conversation had them", () => {
    const items = timelineOf(
      conversation(
        { turn_index: 0, events: [event("user", { text: "first" })] },
        { turn_index: 1, events: [event("agent", { text: "second" })] },
      ),
    );

    expect(items.map((i) => (i.kind === "text" ? i.text : null))).toEqual([
      "first",
      "second",
    ]);
  });

  it("merges one author's consecutive text into a single bubble", () => {
    const items = timelineOf(
      conversation({
        turn_index: 0,
        events: [
          event("agent", { text: "part one" }),
          event("agent", { text: "part two" }),
        ],
      }),
    );

    expect(items).toEqual([
      {
        kind: "text",
        turnIndex: 0,
        author: "agent",
        text: "part one\npart two",
      },
    ]);
  });

  it("breaks the bubble when the author changes inside one turn", () => {
    // An ADK turn holds both sides of the exchange. Merging on turn alone
    // would render the user's prompt and the agent's reply as one bubble
    // attributed to whoever spoke first.
    const items = timelineOf(
      conversation({
        turn_index: 0,
        events: [
          event("user", { text: "ask" }),
          event("agent", { text: "answer" }),
        ],
      }),
    );

    expect(items).toEqual([
      { kind: "text", turnIndex: 0, author: "user", text: "ask" },
      { kind: "text", turnIndex: 0, author: "agent", text: "answer" },
    ]);
  });

  it("places a tool call where it happened, between the text around it", () => {
    // The whole reason the nested shape is served rather than two flat tables:
    // the fork's schema could only put every bubble of a turn before its tools.
    const items = timelineOf(
      conversation({
        turn_index: 0,
        events: [
          event("agent", { text: "checking" }),
          event("agent", {
            function_call: { id: "c1", name: "lookup", args: { q: 1 } },
          }),
          event("agent", {
            function_response: {
              id: "c1",
              name: "lookup",
              response: { ok: true },
            },
          }),
          event("agent", { text: "done" }),
        ],
      }),
    );

    expect(items.map((i) => i.kind)).toEqual(["text", "toolGroup", "text"]);
  });

  it("pairs a call with its response and reads the tool's own error", () => {
    const items = timelineOf(
      conversation({
        turn_index: 0,
        events: [
          event("agent", {
            function_call: { id: "c1", name: "lookup", args: { q: 1 } },
          }),
          event("agent", {
            function_response: {
              id: "c1",
              name: "lookup",
              response: { error: "nope" },
            },
          }),
        ],
      }),
    );

    expect(items).toHaveLength(1);
    expect(items[0].kind === "toolGroup" && items[0].calls).toEqual([
      {
        callId: "c1",
        turnIndex: 0,
        toolName: "lookup",
        args: { q: 1 },
        response: { error: "nope" },
        isError: true,
      },
    ]);
  });

  it("keeps a response whose call was truncated away", () => {
    const items = timelineOf(
      conversation({
        turn_index: 0,
        events: [
          event("agent", {
            function_response: {
              id: "c9",
              name: "lookup",
              response: { ok: true },
            },
          }),
        ],
      }),
    );

    expect(
      items[0].kind === "toolGroup" && items[0].calls[0].args,
    ).toBeUndefined();
    expect(items[0].kind === "toolGroup" && items[0].calls[0].toolName).toBe(
      "lookup",
    );
  });

  it("does not mark an empty response on a turn that called a tool", () => {
    const items = timelineOf(
      conversation({
        turn_index: 0,
        events: [
          event("agent", { text: "" }),
          event("agent", {
            function_call: { id: "c1", name: "lookup", args: {} },
          }),
        ],
      }),
    );

    expect(items.map((i) => i.kind)).toEqual(["toolGroup"]);
  });

  it("marks a model turn with no text and no tool call, instead of dropping it", () => {
    const items = timelineOf(
      conversation(
        { turn_index: 0, events: [event("user", { text: "do thing" })] },
        { turn_index: 1, events: [event("agent", { text: "" })] },
      ),
    );

    expect(items).toEqual([
      { kind: "text", turnIndex: 0, author: "user", text: "do thing" },
      { kind: "emptyResponse", turnIndex: 1, author: "agent" },
    ]);
  });

  it("falls back to position when a turn carries no index", () => {
    const items = timelineOf(
      conversation({ events: [event("user", { text: "hi" })] }),
    );

    expect(items[0]).toEqual({
      kind: "text",
      turnIndex: 0,
      author: "user",
      text: "hi",
    });
  });
});
