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

// Real GFM parser here (unlike message-bubble.test.tsx) so we exercise how
// fenced code blocks actually render.
import { MessageBubble } from "@/components/chat/message-bubble";
import type { ChatMessage } from "@/components/chat/chat-shell";

function makeMessage(text: string): ChatMessage {
  return {
    id: "m1",
    role: "assistant",
    segments: [{ kind: "text", text }],
    createdAt: Date.now(),
  };
}

const FENCED = ["```python", "def greet():", "    return 'hi'", "```"].join(
  "\n",
);

describe("MessageBubble — fenced code highlighting", () => {
  it("syntax-highlights a fenced code block into colored token spans", () => {
    const { container } = render(
      <MessageBubble message={makeMessage(FENCED)} />,
    );

    const pre = container.querySelector("pre");
    expect(pre).not.toBeNull();
    // Prism tokenizes into many inline-styled spans; plain text would have none.
    const styledTokens = container.querySelectorAll('span[style*="color"]');
    expect(styledTokens.length).toBeGreaterThan(1);
    // A `.token.keyword` (def) proves the python grammar resolved, not a bare
    // "markup" fallback.
    expect(container.querySelector(".token.keyword")).not.toBeNull();
    // The per-block copy affordance is preserved (the whole-message copy also
    // matches, so there is at least one).
    expect(
      screen.getAllByRole("button", { name: /copy/i }).length,
    ).toBeGreaterThan(0);
  });

  it("leaves inline code as plain <code>, not a highlighted block", () => {
    const { container } = render(
      <MessageBubble message={makeMessage("use the `greet()` helper")} />,
    );
    expect(container.querySelector("pre")).toBeNull();
    expect(container.querySelector("code")).not.toBeNull();
    expect(container.querySelectorAll('span[style*="color"]').length).toBe(0);
  });
});
