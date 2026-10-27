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
import userEvent from "@testing-library/user-event";
import { Trajectory } from "../trajectory";
import type { CaseConversation } from "@/lib/aqua-api";
import { fromConversation } from "@/lib/trajectory/fromConversation";
import type { TrajectoryDoc } from "@/lib/trajectory/types";

const DOC: TrajectoryDoc = {
  timingRecorded: false,
  records: [
    {
      id: "r-0",
      index: 0,
      kind: "user",
      turn: 0,
      agent: "user",
      text: "Turn 0 message",
      raw: {},
    },
    {
      id: "r-1",
      index: 1,
      kind: "tool",
      turn: 1,
      agent: "tool",
      text: "",
      toolName: "create_ticket",
      args: { priority: "High" },
      result: { ticket_id: "123" },
      raw: {},
    },
  ],
};

describe("Trajectory", () => {
  it("renders records and toggles detail on click", async () => {
    render(<Trajectory doc={DOC} />);

    expect(screen.getByText("Turn 0 message")).toBeInTheDocument();
    expect(screen.getByText("create_ticket(...)")).toBeInTheDocument();

    const toolRow = screen.getByText("create_ticket(...)");
    await userEvent.click(toolRow);

    // Detail panel opens with tablist and tabs
    expect(
      screen.getByRole("button", { name: "Close detail" }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("tablist", { name: "Trajectory detail tabs" }),
    ).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: "Tools" })).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: "Raw" })).toBeInTheDocument();
    expect(screen.getByText("Arguments")).toBeInTheDocument();

    // Switch to Raw tab
    await userEvent.click(screen.getByRole("tab", { name: "Raw" }));
    expect(screen.getByText(/"toolName": "create_ticket"/)).toBeInTheDocument();

    // Clicking row again closes it
    await userEvent.click(toolRow);
    expect(screen.queryByRole("button", { name: "Close detail" })).toBeNull();
  });

  it("opens Preview and Raw tabs when non-tool record is clicked", async () => {
    render(<Trajectory doc={DOC} />);

    const userRow = screen.getByText("Turn 0 message");
    await userEvent.click(userRow);

    expect(
      screen.getByRole("tablist", { name: "Trajectory detail tabs" }),
    ).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: "Preview" })).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: "Raw" })).toBeInTheDocument();

    // Close detail via close button
    await userEvent.click(screen.getByRole("button", { name: "Close detail" }));
    expect(screen.queryByRole("tablist")).toBeNull();
  });

  it("renders a clean single-line summary in the ledger row and full body in Preview tab", async () => {
    const doc: TrajectoryDoc = {
      timingRecorded: false,
      records: [
        {
          id: "r-sys",
          index: 0,
          kind: "system",
          turn: 0,
          agent: "investigate",
          text: "**Session review:** 3 sessions evaluated.\n\n1 failed.",
          raw: {},
        },
      ],
    };

    render(<Trajectory doc={doc} />);

    const row = screen.getByText(
      "Session review: 3 sessions evaluated. 1 failed.",
    );
    expect(row).toBeInTheDocument();

    await userEvent.click(row);

    expect(screen.getByRole("tab", { name: "Preview" })).toBeInTheDocument();
    expect(screen.getByText("Session review:")).toBeInTheDocument();
    expect(screen.getByText("1 failed.")).toBeInTheDocument();
  });

  it("summarizes a markdown assistant reply from a case conversation", () => {
    const convo: CaseConversation = {
      trajectory_id: "case-1",
      status: "ingested",
      turn_count: 1,
      turns_returned: 1,
      turns: [
        {
          turn_index: 0,
          turn_id: "t0",
          events: [
            {
              author: "agent",
              content: {
                role: "model",
                parts: [
                  {
                    text: '**Ticket filed:** _priority High_\n```json\n{"id": 1}\n```',
                  },
                ],
              },
            },
          ],
        },
      ],
    };

    render(<Trajectory doc={fromConversation(convo)} />);

    expect(screen.getByTestId("ledger-row")).toHaveTextContent(
      "Ticket filed: priority High",
    );
    expect(screen.getByTestId("ledger-row")).not.toHaveTextContent("```");
  });
});
