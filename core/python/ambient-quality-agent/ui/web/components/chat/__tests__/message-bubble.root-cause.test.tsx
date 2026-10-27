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

import { MessageBubble } from "@/components/chat/message-bubble";
import type { ChatMessage, MessageSegment } from "@/components/chat/chat-shell";

const SUMMARY =
  "The instruction covering the refund step reads as a preference, so the " +
  "model treats it as optional.";

const EDIT = {
  path: "app/agent.py",
  start_line: 42,
  end_line: 44,
  before: '    instruction=(\n        "Help the user."\n    ),',
  after:
    '    instruction=(\n        "Help the user. Never skip a refund."\n    ),',
  rationale: "State the refund step as a constraint.",
};

/** Mock tool payload matching raw backend snake_case response structure. */
const RECORDED = {
  recorded: true,
  root_cause_id: "rc-1",
  edits_recorded: 1,
  record: {
    root_cause_id: "rc-1",
    insight_id: "ins-1",
    occurrence_id: "occ-1",
    agent_revision: "rev-9",
    summary: SUMMARY,
    edits: [EDIT],
    created_at: "2026-01-02T03:04:05Z",
  },
  warnings: [],
};

const REJECTED = {
  recorded: false,
  anchored: [],
  rejected: [
    {
      path: "app/missing.py",
      start_line: 10,
      end_line: 12,
      reason: "app/missing.py is not in the snapshot for revision rev-9.",
    },
  ],
  warnings: [],
};

function toolSegment(result: unknown): MessageSegment {
  return {
    kind: "tool",
    callId: "call-1",
    name: "record_root_cause",
    args: { insight_id: "ins-1", edits: [EDIT] },
    result,
    hasResult: true,
  } as MessageSegment;
}

function message(segments: MessageSegment[]): ChatMessage {
  return {
    id: "m1",
    role: "assistant",
    segments,
    createdAt: Date.now(),
  };
}

describe("MessageBubble — record_root_cause", () => {
  it("renders the stored record open, with no click needed", () => {
    render(<MessageBubble message={message([toolSegment(RECORDED)])} />);

    expect(screen.getByTestId("root-cause-record")).toBeInTheDocument();
    expect(screen.getByText(SUMMARY)).toBeInTheDocument();
    // Verify target file path and line range header are displayed.
    expect(screen.getByText("app/agent.py:42-44")).toBeInTheDocument();
    expect(screen.getByText(EDIT.rationale)).toBeInTheDocument();
  });

  it("renders the diff from the result's anchored before text", () => {
    const { container } = render(
      <MessageBubble message={message([toolSegment(RECORDED)])} />,
    );
    const lines = Array.from(container.querySelectorAll("pre > div")).map(
      (d) => d.textContent ?? "",
    );
    expect(lines).toContain('-        "Help the user."');
    expect(lines).toContain('+        "Help the user. Never skip a refund."');
  });

  it("stays out of the collapsed tool group its neighbours form", () => {
    const neighbours: MessageSegment[] = [
      {
        kind: "tool",
        callId: "a",
        name: "read",
        args: { path: "app/agent.py" },
        result: {},
        hasResult: true,
      } as MessageSegment,
      {
        kind: "tool",
        callId: "b",
        name: "read",
        args: { path: "app/tools.py" },
        result: {},
        hasResult: true,
      } as MessageSegment,
      toolSegment(RECORDED),
    ];
    render(<MessageBubble message={message(neighbours)} />);

    // Standard tool calls collapse into a group.
    expect(screen.getByText("2 tool calls")).toBeInTheDocument();
    // Root-cause recordings remain uncollapsed for immediate visibility.
    expect(screen.getByTestId("root-cause-record")).toBeInTheDocument();
    expect(screen.getByText(SUMMARY)).toBeInTheDocument();
  });

  it("previews the proposed edits on the chip", () => {
    render(<MessageBubble message={message([toolSegment(RECORDED)])} />);
    expect(screen.getByText("1 edit · app/agent.py")).toBeInTheDocument();
  });

  it("reports the refusals when nothing was recorded", () => {
    render(<MessageBubble message={message([toolSegment(REJECTED)])} />);

    expect(screen.queryByTestId("root-cause-record")).toBeNull();
    expect(screen.getByTestId("root-cause-rejected")).toBeInTheDocument();
    expect(screen.getByText(/No root cause was recorded/)).toBeInTheDocument();
    expect(screen.getByText("app/missing.py:10-12")).toBeInTheDocument();
    expect(
      screen.getByText(/is not in the snapshot for revision rev-9/),
    ).toBeInTheDocument();
  });
});
