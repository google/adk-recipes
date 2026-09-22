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
import { render, screen, fireEvent } from "@testing-library/react";

// Stub react-markdown to a plain <div> so we can match text without paying
// for the parser. Same trick remark-gfm goes through the same module.
vi.mock("react-markdown", () => ({
  default: ({ children }: { children: string }) => (
    <div data-testid="markdown">{children}</div>
  ),
}));
vi.mock("remark-gfm", () => ({ default: () => null }));

import { MessageBubble } from "@/components/chat/message-bubble";
import type { ChatMessage } from "@/components/chat/chat-shell";
import { ViewerProvider } from "@/components/chat/artifact-viewer/viewer-context";
import { ArtifactViewer } from "@/components/chat/artifact-viewer/artifact-viewer";

function makeMessage(overrides: Partial<ChatMessage>): ChatMessage {
  return {
    id: "m1",
    role: "assistant",
    segments: [],
    createdAt: Date.now(),
    ...overrides,
  };
}

describe("MessageBubble — existing segment kinds", () => {
  it("renders text segments via the markdown stub", () => {
    render(
      <MessageBubble
        message={makeMessage({
          segments: [{ kind: "text", text: "Hello world" }],
        })}
      />,
    );
    expect(screen.getByTestId("markdown")).toHaveTextContent("Hello world");
  });

  it("renders system messages with the dashed-border style", () => {
    render(
      <MessageBubble
        message={makeMessage({
          role: "system",
          segments: [{ kind: "text", text: "System notice" }],
        })}
      />,
    );
    expect(screen.getByText("System notice")).toBeInTheDocument();
  });

  it("renders a tool segment as a collapsed row showing the tool name", () => {
    render(
      <MessageBubble
        message={makeMessage({
          segments: [
            { kind: "tool", callId: "c1", name: "web_search", args: { q: "x" } },
          ],
        })}
      />,
    );
    expect(screen.getByText("web_search")).toBeInTheDocument();
    // Args + result are hidden until the user expands the row.
    expect(screen.queryByText(/args/)).toBeNull();
  });

  it("expands a tool row on click to reveal its body", () => {
    render(
      <MessageBubble
        message={makeMessage({
          segments: [
            { kind: "tool", callId: "c1", name: "web_search", args: { q: "x" } },
          ],
        })}
      />,
    );
    expect(screen.queryByText("args")).toBeNull();
    fireEvent.click(screen.getByText("web_search"));
    expect(screen.getByText("args")).toBeInTheDocument();
  });

  it("renders load_skill with a 'load_skill' label and the skill name as preview", () => {
    render(
      <MessageBubble
        message={makeMessage({
          segments: [
            {
              kind: "tool",
              callId: "c1",
              name: "load_skill",
              // skill_name is the tool's real arg name, not name
              // (final-review Fix 9).
              args: { skill_name: "plotting" },
            },
          ],
        })}
      />,
    );
    expect(screen.getByText("load_skill")).toBeInTheDocument();
    expect(screen.getByText("plotting")).toBeInTheDocument();
  });

  it("groups consecutive tool segments behind a collapsed 'N tool calls' header", () => {
    render(
      <MessageBubble
        message={makeMessage({
          segments: [
            { kind: "tool", callId: "a", name: "a", args: null },
            { kind: "tool", callId: "b", name: "b", args: null },
            { kind: "tool", callId: "c", name: "c", args: null },
          ],
        })}
      />,
    );
    // Header shows the count; individual rows are hidden until expanded.
    expect(screen.getByText(/3\s+tool calls/i)).toBeInTheDocument();
    expect(screen.queryByText("a")).toBeNull();
    expect(screen.queryByText("b")).toBeNull();
    expect(screen.queryByText("c")).toBeNull();
  });

  it("renders a single tool segment as a bare row, no outer group header", () => {
    render(
      <MessageBubble
        message={makeMessage({
          segments: [
            { kind: "tool", callId: "only", name: "lonely_tool", args: null },
          ],
        })}
      />,
    );
    expect(screen.queryByText(/tool calls/i)).toBeNull();
    expect(screen.getByText("lonely_tool")).toBeInTheDocument();
  });

  it("groups only consecutive tools and lets text segments break the group", () => {
    render(
      <MessageBubble
        message={makeMessage({
          segments: [
            { kind: "text", text: "before" },
            { kind: "tool", callId: "a", name: "a", args: null },
            { kind: "text", text: "middle" },
            { kind: "tool", callId: "b", name: "b", args: null },
            { kind: "tool", callId: "c", name: "c", args: null },
            { kind: "text", text: "after" },
          ],
        })}
      />,
    );
    // Text segments stay inline in turn order.
    expect(screen.getAllByTestId("markdown")).toHaveLength(3);
    // 'a' is alone in its group → renders as a bare row (name visible).
    expect(screen.getByText("a")).toBeInTheDocument();
    // 'b' and 'c' are consecutive → collapsed behind a "2 tool calls" header.
    expect(screen.getByText(/2\s+tool calls/i)).toBeInTheDocument();
    expect(screen.queryByText("b")).toBeNull();
    expect(screen.queryByText("c")).toBeNull();
  });

  it("renders a text/html file part as a chip that opens in the viewer", () => {
    render(
      <ViewerProvider>
        <MessageBubble
          message={makeMessage({
            segments: [
              {
                kind: "file",
                file: {
                  mimeType: "text/html",
                  bytes: btoa("<h1>hi</h1>"),
                  name: "r.html",
                },
              },
            ],
          })}
        />
        <ArtifactViewer />
      </ViewerProvider>,
    );
    // Files never render inline: no iframe until the user opens the chip.
    expect(document.querySelector("iframe")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: /r\.html/i }));
    expect(screen.getByRole("tab", { name: /r\.html/i })).toBeTruthy();
  });

  it("renders a data segment as a JSON pre block", () => {
    render(
      <MessageBubble
        message={makeMessage({
          segments: [{ kind: "data", mime: "application/json", data: { x: 1 } }],
        })}
      />,
    );
    // The pre block contains the prefix line "data · application/json" plus the JSON
    expect(screen.getByText(/application\/json/)).toBeInTheDocument();
  });

  it("renders an image file as a chip that opens in the viewer (not inline)", () => {
    render(
      <ViewerProvider>
        <MessageBubble
          message={makeMessage({
            segments: [
              {
                kind: "file",
                file: { bytes: "AAAA", mimeType: "image/png", name: "x.png" },
              },
            ],
          })}
        />
        <ArtifactViewer />
      </ViewerProvider>,
    );
    // No inline image in the chat until the chip is opened.
    expect(screen.queryByAltText("x.png")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: /x\.png/i }));
    const img = screen.getByAltText("x.png");
    expect(img.getAttribute("src")).toContain("data:image/png;base64,AAAA");
  });

  it("wraps assistant text in an aria-live region for screen reader streaming", () => {
    render(
      <MessageBubble
        message={makeMessage({
          segments: [{ kind: "text", text: "Hello" }],
        })}
      />,
    );
    // The aria-live wrapper is two ancestors up from the markdown stub:
    // <div aria-live=polite> → <Markdown wrapper div> → <div data-testid=markdown>
    const live = screen.getByTestId("markdown").closest("[aria-live]");
    expect(live).not.toBeNull();
    expect(live).toHaveAttribute("aria-live", "polite");
    expect(live).toHaveAttribute("aria-atomic", "false");
  });

  it("does NOT mark user text with an aria-live region", () => {
    render(
      <MessageBubble
        message={makeMessage({
          role: "user",
          segments: [{ kind: "text", text: "hi" }],
        })}
      />,
    );
    expect(screen.getByTestId("markdown").closest("[aria-live]")).toBeNull();
  });

  it("hides the time-ago label for replayed messages with synthetic timestamps", () => {
    // task-to-segments uses tiny counter values (0, 1, 1000…) purely for
    // ordering — rendering those as wall-clock produced "20598d ago".
    render(
      <MessageBubble
        message={makeMessage({
          segments: [{ kind: "text", text: "Replayed" }],
          createdAt: 42,
        })}
      />,
    );
    expect(screen.queryByText(/ago|just now/)).toBeNull();
  });

  it("shows the time-ago label for live messages with real timestamps", () => {
    render(
      <MessageBubble
        message={makeMessage({
          segments: [{ kind: "text", text: "Live" }],
          createdAt: Date.now(),
        })}
      />,
    );
    expect(screen.getByText(/ago|just now/)).toBeInTheDocument();
  });
});
