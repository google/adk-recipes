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

vi.mock("react-markdown", () => ({ default: ({ children }: { children: string }) => <div>{children}</div> }));
vi.mock("remark-gfm", () => ({ default: () => null }));

const confirmMock = vi.fn();
vi.mock("@/components/chat/confirmation-context", () => ({
  useConfirmationSender: () => confirmMock,
}));

import { MessageBubble } from "@/components/chat/message-bubble";
import type { ChatMessage } from "@/components/chat/chat-shell";

function perm(callId: string, summary: string) {
  return {
    kind: "confirmation" as const,
    callId,
    hint: summary,
    originalCall: { name: "bash", args: { command: summary } },
    payload: { kind: "permission", toolName: "bash", summary, proposedRule: {}, choices: ["Yes, once", "x", "y", "Decline"] },
  };
}

function msg(segments: ChatMessage["segments"]): ChatMessage {
  return { id: "m", role: "assistant", segments, createdAt: Date.now() };
}

describe("CombinedPermissionCard", () => {
  it("renders one card for 2+ pending permission cards and batches 'This session'", async () => {
    confirmMock.mockReset();
    const user = userEvent.setup();
    render(<MessageBubble message={msg([perm("c1", "Run: gmail"), perm("c2", "Run: chat")])} />);

    expect(screen.getByText("Run: gmail")).toBeInTheDocument();
    expect(screen.getByText("Run: chat")).toBeInTheDocument();

    await user.click(screen.getByText("This session"));
    expect(confirmMock).toHaveBeenCalledTimes(1);
    expect(confirmMock).toHaveBeenCalledWith([
      { callId: "c1", confirmed: true, payload: { outcome: "proceed_always" } },
      { callId: "c2", confirmed: true, payload: { outcome: "proceed_always" } },
    ]);
  });

  it("batches Decline as confirmed=false / cancel", async () => {
    confirmMock.mockReset();
    const user = userEvent.setup();
    render(<MessageBubble message={msg([perm("c1", "Run: gmail"), perm("c2", "Run: chat")])} />);
    await user.click(screen.getByText("Decline"));
    expect(confirmMock).toHaveBeenCalledWith([
      { callId: "c1", confirmed: false, payload: { outcome: "cancel" } },
      { callId: "c2", confirmed: false, payload: { outcome: "cancel" } },
    ]);
  });

  it("does NOT combine a single pending permission card", () => {
    render(<MessageBubble message={msg([perm("c1", "Run: solo")])} />);
    // single PermissionCard shows the server-provided choices, not the combined buttons
    expect(screen.getByText("Yes, once")).toBeInTheDocument();
    expect(screen.queryByText("This session")).not.toBeInTheDocument();
  });

  it("batches 'Yes, once' as confirmed=true / proceed_once", async () => {
    confirmMock.mockReset();
    const user = userEvent.setup();
    render(<MessageBubble message={msg([perm("c1", "Run: gmail"), perm("c2", "Run: chat")])} />);
    await user.click(screen.getByText("Yes, once"));
    expect(confirmMock).toHaveBeenCalledWith([
      { callId: "c1", confirmed: true, payload: { outcome: "proceed_once" } },
      { callId: "c2", confirmed: true, payload: { outcome: "proceed_once" } },
    ]);
  });

  it("does NOT combine when a clarify segment breaks the run", () => {
    const clarifySeg = { kind: "clarify" as const, callId: "cl", question: "pick", choices: ["a", "b"] };
    render(<MessageBubble message={msg([perm("c1", "Run: gmail"), clarifySeg, perm("c2", "Run: chat")])} />);
    // no combined header + individual permission summaries visible
    expect(screen.queryByText(/actions need approval/i)).not.toBeInTheDocument();
    expect(screen.getByText("Run: gmail")).toBeInTheDocument();
    expect(screen.getByText("Run: chat")).toBeInTheDocument();
  });
});
