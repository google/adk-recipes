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
import type { CaseConversation } from "@/lib/aqua-api";
import { fromConversation } from "../fromConversation";

const CONVERSATION: CaseConversation = {
  trajectory_id: "aqa-017-nopri-wifi-20260821-143203-0ebe11",
  status: "ingested",
  turn_count: 3,
  turns_returned: 3,
  turns: [
    {
      turn_index: 0,
      turn_id: "t0",
      events: [
        {
          author: "user",
          content: { role: "user", parts: [{ text: "My wifi is down." }] },
        },
      ],
    },
    {
      turn_index: 1,
      turn_id: "t1",
      events: [
        {
          author: "agent",
          content: {
            role: "model",
            parts: [{ text: "I'll open a ticket for you." }],
          },
        },
        {
          author: "agent",
          content: {
            role: "model",
            parts: [
              {
                function_call: {
                  id: "call-1",
                  name: "create_ticket",
                  args: { title: "wifi down", priority: "Urgent" },
                },
              },
            ],
          },
        },
        {
          author: "agent",
          content: {
            role: "model",
            parts: [
              {
                function_response: {
                  id: "call-1",
                  name: "create_ticket",
                  response: {
                    error: "ValueError: priority must be High, Medium, Low",
                  },
                },
              },
            ],
          },
        },
      ],
    },
    {
      turn_index: 2,
      turn_id: "t2",
      events: [
        { author: "agent", content: { role: "model", parts: [{ text: "" }] } },
      ],
    },
  ],
};

describe("fromConversation", () => {
  it("gives every turn and tool call its own record, in turn order", () => {
    const doc = fromConversation(CONVERSATION);

    expect(doc.records).toHaveLength(4);
    expect(doc.records.map((r) => r.kind)).toEqual([
      "user",
      "assistant",
      "tool",
      "assistant",
    ]);
    expect(doc.timingRecorded).toBe(false);
  });

  it("carries a tool call's args and result through as values", () => {
    // They arrive as objects now, not as JSON strings a reader has to parse:
    // the archive stores the part as `genai` produced it.
    const doc = fromConversation(CONVERSATION);
    const tool = doc.records.find((r) => r.kind === "tool");

    expect(tool?.toolName).toBe("create_ticket");
    expect(tool?.args).toEqual({ title: "wifi down", priority: "Urgent" });
    expect(tool?.result).toEqual({
      error: "ValueError: priority must be High, Medium, Low",
    });
    expect(tool?.isError).toBe(true);
  });

  it("marks the silently-failed turn instead of producing an empty record", () => {
    const doc = fromConversation(CONVERSATION);
    const last = doc.records[3];

    expect(last.kind).toBe("assistant");
    expect(last.text).toBe("(no response)");
  });
});
