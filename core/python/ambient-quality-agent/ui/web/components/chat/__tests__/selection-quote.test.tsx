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

import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { MessageBubble } from "../message-bubble";
import { ViewerProvider } from "../artifact-viewer/viewer-context";
import { SELECTION_DATA_KIND } from "../workspace-href";
import type { ChatMessage } from "../chat-shell";

function bubble(segments: unknown[]) {
  const message = {
    id: "m1",
    role: "user",
    createdAt: 0,
    segments,
  } as unknown as ChatMessage;
  return render(
    <ViewerProvider>
      <MessageBubble message={message} />
    </ViewerProvider>,
  );
}

const selection = {
  lha_kind: SELECTION_DATA_KIND,
  path: "/workspace/cats.md",
  start: 40,
  end: 46,
  start_line: 6,
  end_line: 6,
  snippet: "# dogs",
};

describe("selection quote in a user bubble", () => {
  it("shows the file, the line, and the snippet", () => {
    bubble([
      { kind: "data", mime: undefined, data: selection },
      { kind: "text", text: "make upper case the first letter" },
    ]);
    expect(screen.getByText("cats.md")).toBeTruthy();
    expect(screen.getByText(/line 6/)).toBeTruthy();
    expect(screen.getByText("# dogs")).toBeTruthy();
    expect(screen.getByText(/make upper case the first letter/)).toBeTruthy();
  });

  it("shows the snippet verbatim rather than rendering it as markdown", () => {
    // The old text-prefix approach turned "# dogs" into an <h1>.
    bubble([{ kind: "data", mime: undefined, data: selection }]);
    expect(screen.queryByRole("heading")).toBeNull();
  });

  it("leaks none of the model-facing framing", () => {
    bubble([{ kind: "data", mime: undefined, data: selection }]);
    const text = document.body.textContent ?? "";
    expect(text).not.toContain("<<<");
    expect(text).not.toContain("lha_kind");
  });

  it("reads as a range when the selection spans lines", () => {
    bubble([
      {
        kind: "data",
        mime: undefined,
        data: { ...selection, start_line: 6, end_line: 8 },
      },
    ]);
    expect(screen.getByText(/lines 6–8/)).toBeTruthy();
  });

  it("opens the file under the path every other route uses", () => {
    // A different string here would key a second editable tab for the same
    // file, each autosaving over the other.
    bubble([{ kind: "data", mime: undefined, data: selection }]);
    const btn = screen.getByRole("button", { name: "cats.md" });
    expect(btn.getAttribute("title")).toBe(
      "Open /workspace/cats.md in the side panel",
    );
  });

  it("still dumps an unrecognised data part as JSON", () => {
    bubble([{ kind: "data", mime: undefined, data: { foo: "bar" } }]);
    expect(screen.getByText(/"foo": "bar"/)).toBeTruthy();
  });
});
