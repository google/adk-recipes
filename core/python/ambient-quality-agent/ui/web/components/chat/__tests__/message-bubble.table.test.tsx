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

// Unlike the sibling message-bubble.test.tsx, this file does NOT mock
// react-markdown — we want the real GFM parser so we can assert how tables
// are actually rendered into the DOM.
import { MessageBubble } from "@/components/chat/message-bubble";
import type { ChatMessage } from "@/components/chat/chat-shell";

const TABLE_MD = [
  "| Col A | Col B | Col C |",
  "| --- | --- | --- |",
  "| 1 | 2 | 3 |",
].join("\n");

function makeMessage(overrides: Partial<ChatMessage>): ChatMessage {
  return {
    id: "m1",
    role: "assistant",
    segments: [],
    createdAt: Date.now(),
    ...overrides,
  };
}

describe("MessageBubble — markdown rendering on narrow viewports", () => {
  it("wraps tables in a horizontally scrollable container so they don't burst the bubble on mobile", () => {
    render(
      <MessageBubble
        message={makeMessage({ segments: [{ kind: "text", text: TABLE_MD }] })}
      />,
    );

    const table = screen.getByRole("table");
    const wrapper = table.parentElement;
    expect(wrapper).not.toBeNull();
    // Standalone class tokens — not the prose-pre:* modifiers that live on the
    // outer prose container (which would false-match a substring regex).
    const wrapperClasses = (wrapper?.className ?? "").split(/\s+/);
    expect(wrapperClasses).toContain("overflow-x-auto");
    expect(wrapperClasses).toContain("max-w-full");
  });

  it("lets long link text wrap instead of overflowing the bubble", () => {
    const longUrl =
      "https://example.com/some/really/long/path/that/will/not/fit/on/a/phone/screen/at/all";
    render(
      <MessageBubble
        message={makeMessage({
          segments: [{ kind: "text", text: `[${longUrl}](${longUrl})` }],
        })}
      />,
    );

    const link = screen.getByRole("link");
    expect(link.className).toMatch(/overflow-wrap:anywhere/);
  });

  it("constrains inline markdown images to the bubble width", () => {
    render(
      <MessageBubble
        message={makeMessage({
          segments: [
            { kind: "text", text: "![alt](https://example.com/x.png)" },
          ],
        })}
      />,
    );

    const img = screen.getByRole("img");
    const classes = img.className.split(/\s+/);
    expect(classes).toContain("max-w-full");
    expect(classes).toContain("h-auto");
  });
});
