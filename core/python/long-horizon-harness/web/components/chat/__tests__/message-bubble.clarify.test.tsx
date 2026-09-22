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
    id: "m-clarify",
    role: "assistant",
    segments: [],
    createdAt: Date.now(),
    ...overrides,
  };
}

describe("MessageBubble — clarify segment", () => {
  it("renders the question and one button per choice", () => {
    render(
      <MessageBubble
        message={makeMessage({
          segments: [
            {
              kind: "clarify",
              callId: "call-1",
              question: "What kind of plot would you like?",
              choices: ["line", "bar", "scatter"],
            },
          ],
        })}
      />,
    );
    expect(
      screen.getByText("What kind of plot would you like?"),
    ).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "line" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "bar" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "scatter" })).toBeInTheDocument();
  });

  it("invokes the confirmation sender with the chosen value when a button is clicked", async () => {
    confirmMock.mockReset();
    const user = userEvent.setup();
    render(
      <MessageBubble
        message={makeMessage({
          segments: [
            {
              kind: "clarify",
              callId: "call-1",
              question: "Pick one",
              choices: ["line", "bar"],
            },
          ],
        })}
      />,
    );
    await user.click(screen.getByRole("button", { name: "bar" }));
    expect(confirmMock).toHaveBeenCalledTimes(1);
    expect(confirmMock).toHaveBeenCalledWith({
      callId: "call-1",
      confirmed: true,
      payload: { choice: "bar" },
    });
  });

  it("falls back to a textarea + Send when choices is empty", async () => {
    confirmMock.mockReset();
    const user = userEvent.setup();
    render(
      <MessageBubble
        message={makeMessage({
          segments: [
            {
              kind: "clarify",
              callId: "call-2",
              question: "What's the URL?",
              choices: [],
            },
          ],
        })}
      />,
    );
    const textarea = screen.getByRole("textbox");
    await user.type(textarea, "https://example.com");
    await user.click(screen.getByRole("button", { name: /send/i }));
    expect(confirmMock).toHaveBeenCalledWith({
      callId: "call-2",
      confirmed: true,
      payload: { answer: "https://example.com" },
    });
  });

  it("renders a clarify card inline and leaves neighboring tools as bare rows", () => {
    render(
      <MessageBubble
        message={makeMessage({
          segments: [
            { kind: "tool", callId: "t1", name: "before", args: null },
            {
              kind: "clarify",
              callId: "c1",
              question: "Question here",
              choices: ["a"],
            },
            { kind: "tool", callId: "t2", name: "after", args: null },
          ],
        })}
      />,
    );
    // The clarify card always renders inline — it's user-facing output.
    expect(screen.getByText("Question here")).toBeInTheDocument();
    // Clarify breaks the tool group, so each tool is alone in its group and
    // renders as a bare row.
    expect(screen.queryByText(/tool calls/i)).toBeNull();
    expect(screen.getByText("before")).toBeInTheDocument();
    expect(screen.getByText("after")).toBeInTheDocument();
  });
});

describe("MessageBubble — confirmation segment", () => {
  it("renders the hint, original call name, and Approve/Decline buttons", () => {
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
            },
          ],
        })}
      />,
    );
    expect(
      screen.getByText("Run shell command: ls /tmp ?"),
    ).toBeInTheDocument();
    expect(screen.getByText("bash")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /approve/i })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /decline/i })).toBeInTheDocument();
  });

  it("sends confirmed=true with the payload on Approve", async () => {
    confirmMock.mockReset();
    const user = userEvent.setup();
    render(
      <MessageBubble
        message={makeMessage({
          segments: [
            {
              kind: "confirmation",
              callId: "conf-1",
              hint: "Do the thing?",
              originalCall: { name: "bash", args: null },
              payload: { extra: 1 },
            },
          ],
        })}
      />,
    );
    await user.click(screen.getByRole("button", { name: /approve/i }));
    expect(confirmMock).toHaveBeenCalledWith({
      callId: "conf-1",
      confirmed: true,
      payload: { extra: 1 },
    });
  });

  it("sends confirmed=false on Decline", async () => {
    confirmMock.mockReset();
    const user = userEvent.setup();
    render(
      <MessageBubble
        message={makeMessage({
          segments: [
            {
              kind: "confirmation",
              callId: "conf-2",
              hint: "Do the thing?",
              originalCall: null,
              payload: null,
            },
          ],
        })}
      />,
    );
    await user.click(screen.getByRole("button", { name: /decline/i }));
    expect(confirmMock).toHaveBeenCalledWith({
      callId: "conf-2",
      confirmed: false,
      payload: null,
    });
  });
});
