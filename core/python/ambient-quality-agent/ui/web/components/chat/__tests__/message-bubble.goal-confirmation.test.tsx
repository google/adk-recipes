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

vi.mock("@/components/chat/confirmation-context", () => ({
  useConfirmationSender: () => vi.fn(),
}));

import { MessageBubble } from "@/components/chat/message-bubble";
import type { ChatMessage } from "@/components/chat/chat-shell";

const GOAL = "Look hardest at refunds.\nTone does not matter.";

function renderCard(
  goal: string,
  answer?: { confirmed: boolean; text: string | null },
) {
  const message: ChatMessage = {
    id: "m-goal",
    role: "assistant",
    createdAt: Date.now(),
    segments: [
      {
        kind: "confirmation",
        callId: "conf-goal",
        hint: goal.trim()
          ? "Save this as the developer goal?"
          : "Remove the developer goal?",
        originalCall: { name: "set_goal", args: { goal } },
        payload: null,
        answer,
      },
    ],
  };
  return render(<MessageBubble message={message} />);
}

describe("MessageBubble — set_goal confirmation", () => {
  it("shows the whole goal, line breaks kept, above Approve", () => {
    renderCard(GOAL);

    // The user approves what they can read: every line of the text saved.
    expect(screen.getByLabelText("Goal to save").textContent).toBe(GOAL);
    expect(
      screen.getByRole("button", { name: /approve/i }),
    ).toBeInTheDocument();
  });

  it("says what removing the goal does", () => {
    renderCard("");

    expect(screen.queryByLabelText("Goal to save")).toBeNull();
    expect(
      screen.getByText(
        /Investigations run without a goal until you save another/,
      ),
    ).toBeInTheDocument();
    expect(screen.getByText(/stays in Previous versions/)).toBeInTheDocument();
  });

  it("keeps showing the goal once answered", () => {
    renderCard(GOAL, { confirmed: true, text: null });

    expect(screen.getByLabelText("Goal to save").textContent).toBe(GOAL);
    expect(screen.getByText(/Approved/)).toBeInTheDocument();
  });
});
