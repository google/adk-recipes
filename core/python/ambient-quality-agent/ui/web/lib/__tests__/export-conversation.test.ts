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
import {
  conversationToJson,
  conversationToMarkdown,
} from "@/lib/export-conversation";
import type { ChatMessage } from "@/components/chat/chat-shell";

const AT = Date.UTC(2026, 0, 2, 3, 4, 5);

function msg(over: Partial<ChatMessage>): ChatMessage {
  return { id: "x", role: "assistant", segments: [], createdAt: AT, ...over };
}

describe("conversationToMarkdown", () => {
  it("renders role headings and text for each message", () => {
    const md = conversationToMarkdown(
      [
        msg({
          id: "1",
          role: "user",
          segments: [{ kind: "text", text: "hi there" }],
        }),
        msg({
          id: "2",
          role: "assistant",
          segments: [{ kind: "text", text: "hello back" }],
        }),
      ],
      "My chat",
    );
    expect(md).toContain("# My chat");
    expect(md).toMatch(/## User/);
    expect(md).toContain("hi there");
    expect(md).toMatch(/## Assistant/);
    expect(md).toContain("hello back");
  });

  it("summarizes a tool call on its own line", () => {
    const md = conversationToMarkdown([
      msg({
        segments: [
          {
            kind: "tool",
            callId: "c",
            name: "web_search",
            args: { q: "cats" },
            hasResult: true,
          },
        ],
      }),
    ]);
    expect(md).toContain("# Chat");
    expect(md).toMatch(/web_search/);
    expect(md).toContain("cats");
  });

  it("references a file segment by name", () => {
    const md = conversationToMarkdown([
      msg({
        segments: [
          {
            kind: "file",
            file: { name: "report.pdf", mimeType: "application/pdf" },
          },
        ],
      }),
    ]);
    expect(md).toContain("report.pdf");
  });

  it("renders a fenced code block for a text segment unchanged", () => {
    const md = conversationToMarkdown([
      msg({ segments: [{ kind: "text", text: "```py\nx=1\n```" }] }),
    ]);
    expect(md).toContain("```py");
    expect(md).toContain("x=1");
  });
});

describe("conversationToJson", () => {
  it("returns the messages array as pretty JSON round-trippable to the input", () => {
    const messages = [
      msg({ id: "1", role: "user", segments: [{ kind: "text", text: "hi" }] }),
    ];
    const json = conversationToJson(messages);
    expect(JSON.parse(json)).toEqual(messages);
    expect(json).toContain("\n  "); // pretty-printed
  });
});
