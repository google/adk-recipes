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

import { render, screen, fireEvent } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { Markdown } from "../markdown";
import { ArtifactViewer } from "../artifact-viewer/artifact-viewer";
import { ViewerProvider } from "../artifact-viewer/viewer-context";

function setup(text: string) {
  return render(
    <ViewerProvider>
      <Markdown text={text} />
      <ArtifactViewer />
    </ViewerProvider>,
  );
}

describe("markdown workspace links", () => {
  // Opening a tab kicks off the viewer's content fetch; leaving it real
  // makes happy-dom abort it on teardown and log the rejection.
  beforeEach(() => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => new Response("", { status: 200 })),
    );
  });
  afterEach(() => vi.unstubAllGlobals());

  it("opens a bare relative link in the side panel", () => {
    setup("I wrote [cats.md](cats.md) for you.");
    expect(screen.getByText(/no file open/i)).toBeTruthy();

    fireEvent.click(screen.getByRole("button", { name: "cats.md" }));

    // The viewer now owns a tab for it instead of the empty state.
    expect(screen.getByRole("tab", { name: /cats\.md/i })).toBeTruthy();
    expect(screen.queryByText(/no file open/i)).toBeNull();
  });

  it("keeps external links as real anchors in a new tab", () => {
    setup("See [the docs](https://example.com/docs).");
    const link = screen.getByRole("link", { name: "the docs" });
    expect(link.getAttribute("href")).toBe("https://example.com/docs");
    expect(link.getAttribute("target")).toBe("_blank");
  });

  it("does not turn a rooted app link into a panel link", () => {
    setup("Open [sessions](/lha/sessions).");
    expect(screen.getByRole("link", { name: "sessions" })).toBeTruthy();
  });
});
