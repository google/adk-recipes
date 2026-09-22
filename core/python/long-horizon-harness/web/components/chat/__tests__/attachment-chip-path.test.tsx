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

import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { MessageBubble } from "../message-bubble";
import { ArtifactViewer } from "../artifact-viewer/artifact-viewer";
import { ViewerProvider } from "../artifact-viewer/viewer-context";
import { BusyProvider } from "../busy-context";
import { __resetWorkspaceFiles } from "@/lib/workspace-files";
import type { ChatMessage } from "../chat-shell";

const BODY = "# Cats\n\nThey purr.\n";
/** What the message froze at send time; the file has moved on since. */
const STALE = "# STALE\n\nold copy\n";

let tree: { name: string; kind: string; mtime: number }[] = [];

function stubBackend() {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes("/lha/workspace/tree")) {
        return new Response(JSON.stringify({ entries: tree }), { status: 200 });
      }
      return new Response(BODY, { status: 200 });
    }),
  );
}

function attachment(name: string): ChatMessage {
  return {
    id: "m1",
    role: "assistant",
    createdAt: 0,
    segments: [
      {
        kind: "file",
        file: { name, mimeType: "text/markdown", bytes: btoa(STALE) },
      },
    ],
  } as unknown as ChatMessage;
}

function setup(name: string) {
  return render(
    <BusyProvider value={false}>
      <ViewerProvider>
        <MessageBubble message={attachment(name)} />
        <ArtifactViewer />
      </ViewerProvider>
    </BusyProvider>,
  );
}

describe("attachment chip", () => {
  beforeEach(() => {
    __resetWorkspaceFiles();
    tree = [{ name: "cats.md", kind: "file", mtime: 1 }];
    stubBackend();
  });
  afterEach(() => vi.unstubAllGlobals());

  it("opens a workspace file as editable, not a read-only snapshot", async () => {
    setup("cats.md");
    // Wait for the workspace listing, which is what supplies the path.
    await waitFor(() => expect(vi.mocked(fetch)).toHaveBeenCalled());

    fireEvent.click(await screen.findByRole("button", { name: /cats\.md/ }));

    // "Source" is the editable label; "Raw" is the read-only one.
    expect(
      await screen.findByRole("button", { name: /^source$/i }),
    ).toBeTruthy();
    expect(screen.queryByRole("button", { name: /^raw$/i })).toBeNull();
  });

  it("stays read-only for an attachment that is not a workspace file", async () => {
    setup("elsewhere.md");
    await waitFor(() => expect(vi.mocked(fetch)).toHaveBeenCalled());

    fireEvent.click(
      await screen.findByRole("button", { name: /elsewhere\.md/ }),
    );

    expect(await screen.findByRole("button", { name: /^raw$/i })).toBeTruthy();
    expect(screen.queryByRole("button", { name: /^source$/i })).toBeNull();
  });
});

describe("attachment of a file that is still being edited", () => {
  beforeEach(() => {
    __resetWorkspaceFiles();
    tree = [{ name: "cats.md", kind: "file", mtime: 1 }];
    stubBackend();
  });
  afterEach(() => vi.unstubAllGlobals());

  it("shows what is on disk, not the bytes frozen in the message", async () => {
    // The agent has rewritten cats.md since it sent this attachment.
    render(
      <BusyProvider value={false}>
        <ViewerProvider>
          <MessageBubble message={attachment("cats.md")} />
          <ArtifactViewer />
        </ViewerProvider>
      </BusyProvider>,
    );
    await waitFor(() => expect(vi.mocked(fetch)).toHaveBeenCalled());
    fireEvent.click(await screen.findByRole("button", { name: /cats\.md/ }));

    // BODY is what the fake backend serves; the message carries STALE.
    expect(await screen.findByRole("heading", { name: "Cats" })).toBeTruthy();
    expect(screen.queryByText(/STALE/)).toBeNull();
  });
});
