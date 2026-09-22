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
    id: "m-hitl",
    role: "assistant",
    segments: [],
    createdAt: Date.now(),
    ...overrides,
  };
}

describe("MessageBubble — resolved HITL segments", () => {
  it("renders the chosen answer and no choice buttons for an answered clarify", () => {
    render(
      <MessageBubble
        message={makeMessage({
          segments: [
            {
              kind: "clarify",
              callId: "call-1",
              question: "Pick one",
              choices: ["line", "bar"],
              answer: { confirmed: true, text: "bar" },
            },
          ],
        })}
      />,
    );
    expect(screen.getByText(/You answered:\s*bar/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "bar" })).toBeNull();
    expect(screen.queryByRole("button", { name: "line" })).toBeNull();
  });

  it("renders the free-text answer and no textarea for an answered clarify", () => {
    render(
      <MessageBubble
        message={makeMessage({
          segments: [
            {
              kind: "clarify",
              callId: "call-2",
              question: "What's the URL?",
              choices: [],
              answer: { confirmed: true, text: "https://example.com" },
            },
          ],
        })}
      />,
    );
    expect(
      screen.getByText(/You answered:\s*https:\/\/example\.com/),
    ).toBeInTheDocument();
    expect(screen.queryByRole("textbox")).toBeNull();
    expect(screen.queryByRole("button", { name: /send/i })).toBeNull();
  });

  it("renders a Dismissed line for a declined clarify", () => {
    render(
      <MessageBubble
        message={makeMessage({
          segments: [
            {
              kind: "clarify",
              callId: "call-3",
              question: "Pick one",
              choices: ["a", "b"],
              answer: { confirmed: false, text: null },
            },
          ],
        })}
      />,
    );
    expect(screen.getByText(/Dismissed/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "a" })).toBeNull();
  });

  it("renders Approved and no Approve/Decline buttons for an approved confirmation", () => {
    render(
      <MessageBubble
        message={makeMessage({
          segments: [
            {
              kind: "confirmation",
              callId: "conf-1",
              hint: "Run shell command: ls /tmp ?",
              originalCall: { name: "bash", args: { command: "ls /tmp" } },
              payload: null,
              answer: { confirmed: true, text: null },
            },
          ],
        })}
      />,
    );
    expect(screen.getByText(/Approved/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /approve/i })).toBeNull();
    expect(screen.queryByRole("button", { name: /decline/i })).toBeNull();
  });

  it("renders Declined (keeping the tool name pill) for a declined confirmation", () => {
    render(
      <MessageBubble
        message={makeMessage({
          segments: [
            {
              kind: "confirmation",
              callId: "conf-2",
              hint: "Run shell command: ls /tmp ?",
              originalCall: { name: "bash", args: { command: "ls /tmp" } },
              payload: null,
              answer: { confirmed: false, text: null },
            },
          ],
        })}
      />,
    );
    expect(screen.getByText(/Declined/)).toBeInTheDocument();
    expect(screen.getByText("bash")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /approve/i })).toBeNull();
    expect(screen.queryByRole("button", { name: /decline/i })).toBeNull();
  });

  it("still renders interactive controls when the segment is unanswered", () => {
    render(
      <MessageBubble
        message={makeMessage({
          segments: [
            {
              kind: "confirmation",
              callId: "conf-3",
              hint: "Do the thing?",
              originalCall: null,
              payload: null,
            },
          ],
        })}
      />,
    );
    expect(
      screen.getByRole("button", { name: /approve/i }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: /decline/i }),
    ).toBeInTheDocument();
  });
});
