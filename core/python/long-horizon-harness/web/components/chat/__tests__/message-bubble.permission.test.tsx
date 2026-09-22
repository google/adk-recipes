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

import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

vi.mock("react-markdown", () => ({
  default: ({ children }: { children: string }) => (
    <div data-testid="markdown">{children}</div>
  ),
}));
vi.mock("remark-gfm", () => ({ default: () => null }));

const confirmMock = vi.fn();
vi.mock("@/components/chat/confirmation-context", () => ({
  useConfirmationSender: () => confirmMock,
}));

import { MessageBubble } from "@/components/chat/message-bubble";
import type { ChatMessage } from "@/components/chat/chat-shell";

function makeMessage(overrides: Partial<ChatMessage>): ChatMessage {
  return {
    id: "m-perm",
    role: "assistant",
    segments: [],
    createdAt: Date.now(),
    ...overrides,
  };
}

const permSegment = {
  kind: "confirmation" as const,
  callId: "c1",
  hint: "Run: bq rm -t x.y",
  originalCall: { name: "bash", args: { command: "bq rm -t x.y" } },
  payload: {
    kind: "permission",
    toolName: "bash",
    summary: "Run: bq rm -t x.y",
    proposedRule: { toolName: "bash", commandPrefix: ["bq rm"], decision: "allow" },
    choices: [
      "Yes, once",
      "Yes — allow `bq rm` this session",
      "Always allow `bq rm`",
      "Decline",
    ],
  },
};

describe("PermissionCard", () => {
  it("renders the choices and sends the chosen label", async () => {
    confirmMock.mockReset();
    const user = userEvent.setup();
    render(<MessageBubble message={makeMessage({ segments: [permSegment] })} />);
    expect(screen.getByText("Yes, once")).toBeInTheDocument();
    expect(screen.getByText("Always allow `bq rm`")).toBeInTheDocument();

    await user.click(screen.getByText("Yes — allow `bq rm` this session"));
    expect(confirmMock).toHaveBeenCalledWith({
      callId: "c1",
      confirmed: true,
      payload: { choice: "Yes — allow `bq rm` this session" },
    });
  });

  it("sends the trailing Decline choice as confirmed=false", async () => {
    confirmMock.mockReset();
    const user = userEvent.setup();
    render(<MessageBubble message={makeMessage({ segments: [permSegment] })} />);
    await user.click(screen.getByText("Decline"));
    expect(confirmMock).toHaveBeenCalledWith({
      callId: "c1",
      confirmed: false,
      payload: { choice: "Decline" },
    });
  });

  it("shows the chosen label in the resolved card", () => {
    const resolved = {
      ...permSegment,
      answer: { confirmed: true, text: "Always allow `bq rm`" },
    };
    render(<MessageBubble message={makeMessage({ segments: [resolved] })} />);
    expect(screen.getByText("✓ Always allow `bq rm`")).toBeInTheDocument();
  });
});
