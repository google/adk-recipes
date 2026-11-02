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
import { render, screen } from "@testing-library/react";
import type { CaseConversation } from "@/lib/aqua-api";
import { ConversationView } from "../conversation-view";

const CONVERSATION: CaseConversation = {
  trajectory_id: "case-01",
  status: "ingested",
  turn_count: 2,
  turns_returned: 2,
  turns: [
    {
      turn_index: 0,
      turn_id: "t0",
      events: [
        {
          author: "user",
          content: { role: "user", parts: [{ text: "Open a ticket please." }] },
        },
        {
          author: "agent",
          content: {
            role: "model",
            parts: [
              {
                function_call: {
                  id: "c1",
                  name: "create_ticket",
                  args: { priority: "Urgent" },
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
                  id: "c1",
                  name: "create_ticket",
                  response: { error: "ValueError" },
                },
              },
            ],
          },
        },
      ],
    },
    {
      turn_index: 1,
      turn_id: "t1",
      events: [
        { author: "agent", content: { role: "model", parts: [{ text: "" }] } },
      ],
    },
  ],
};

describe("ConversationView", () => {
  it("renders user messages, tool calls, and empty responses", () => {
    render(<ConversationView conversation={CONVERSATION} />);

    expect(screen.getByText("Open a ticket please.")).toBeInTheDocument();
    expect(screen.getByText("create_ticket")).toBeInTheDocument();
    expect(screen.getByText("Error")).toBeInTheDocument();
    expect(screen.getByText("(no response)")).toBeInTheDocument();
  });

  it("shows an empty state when there are no turns or tool calls", () => {
    render(
      <ConversationView
        conversation={{
          trajectory_id: "case-02",
          status: "ingested",
          turn_count: 0,
          turns_returned: 0,
          turns: [],
        }}
      />,
    );
    expect(
      screen.getByText(/This trajectory's artifact has no turns or tool calls/),
    ).toBeInTheDocument();
  });
});
