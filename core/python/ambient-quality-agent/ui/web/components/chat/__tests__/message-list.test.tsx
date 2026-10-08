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

import { beforeAll, describe, expect, it, vi } from "vitest";
import { render } from "@testing-library/react";

// Stub MessageBubble — this test is about the list wrapper's classes, not bubble
// internals (which pull in react-markdown).
vi.mock("@/components/chat/message-bubble", () => ({
  MessageBubble: ({ message }: { message: { id: string } }) => (
    <div data-testid="bubble" data-id={message.id} />
  ),
}));

import { MessageList } from "@/components/chat/message-list";
import type { ChatMessage } from "@/components/chat/chat-shell";

// MessageList's stick-to-bottom effect calls scrollTo; happy-dom has no impl.
beforeAll(() => {
  HTMLElement.prototype.scrollTo = vi.fn();
});

function msg(id: string): ChatMessage {
  return { id, role: "assistant", segments: [], createdAt: 0 };
}

describe("MessageList — viewport/perf attributes", () => {
  it("applies overflow-anchor to the scroll viewport", () => {
    const { container } = render(
      <MessageList messages={[msg("a")]} contextId="c" />,
    );
    const hasAnchor = [...container.querySelectorAll("div")].some((d) =>
      d.className.includes("overflow-anchor:auto"),
    );
    expect(hasAnchor).toBe(true);
  });

  // content-visibility:auto paint-contains the message, which clips the
  // hover edit/regenerate overlay positioned above the bubble. Guard against
  // it being reintroduced.
  it("does not paint-contain messages", () => {
    const { container } = render(
      <MessageList messages={[msg("a"), msg("b")]} contextId="c" />,
    );
    const contained = [...container.querySelectorAll("div")].filter((d) =>
      d.className.includes("content-visibility:auto"),
    );
    expect(contained).toHaveLength(0);
  });
});
