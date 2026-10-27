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
import { FindBar } from "@/components/chat/find-bar";
import type { ChatMessage } from "@/components/chat/chat-shell";

function m(id: string, text: string): ChatMessage {
  return {
    id,
    role: "assistant",
    segments: [{ kind: "text", text }],
    createdAt: 0,
  };
}

const MSGS = [m("a", "the fox"), m("b", "another fox"), m("c", "dog")];

function setup() {
  const onClose = vi.fn();
  const onActiveMatchChange = vi.fn();
  render(
    <FindBar
      messages={MSGS}
      onClose={onClose}
      onActiveMatchChange={onActiveMatchChange}
    />,
  );
  return { onClose, onActiveMatchChange };
}

describe("FindBar", () => {
  it("shows match count and reports the first match as the query is typed", async () => {
    const { onActiveMatchChange } = setup();
    expect(screen.getByPlaceholderText("Find in chat…")).toBeInTheDocument();
    await userEvent.type(
      screen.getByRole("textbox", { name: "Find in chat" }),
      "fox",
    );
    expect(screen.getByText("1/2")).toBeInTheDocument();
    expect(onActiveMatchChange).toHaveBeenLastCalledWith("a");
  });

  it("navigates to the next and previous matches", async () => {
    const { onActiveMatchChange } = setup();
    await userEvent.type(screen.getByRole("textbox", { name: /find/i }), "fox");
    await userEvent.click(screen.getByRole("button", { name: /next match/i }));
    expect(screen.getByText("2/2")).toBeInTheDocument();
    expect(onActiveMatchChange).toHaveBeenLastCalledWith("b");
    await userEvent.click(
      screen.getByRole("button", { name: /previous match/i }),
    );
    expect(screen.getByText("1/2")).toBeInTheDocument();
    expect(onActiveMatchChange).toHaveBeenLastCalledWith("a");
  });

  it("shows 0/0 and clears the active match when nothing matches", async () => {
    const { onActiveMatchChange } = setup();
    await userEvent.type(screen.getByRole("textbox", { name: /find/i }), "zzz");
    expect(screen.getByText("0/0")).toBeInTheDocument();
    expect(onActiveMatchChange).toHaveBeenLastCalledWith(null);
  });

  it("closes on Escape", async () => {
    const { onClose } = setup();
    await userEvent.type(
      screen.getByRole("textbox", { name: /find/i }),
      "{Escape}",
    );
    expect(onClose).toHaveBeenCalled();
  });
});
