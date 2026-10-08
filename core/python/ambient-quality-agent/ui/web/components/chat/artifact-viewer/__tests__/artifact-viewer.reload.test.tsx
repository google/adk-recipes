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

import { StrictMode } from "react";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ArtifactViewer } from "../artifact-viewer";
import { ViewerProvider, useViewer } from "../viewer-context";
import { BusyProvider } from "../../busy-context";

const wsFile = {
  name: "cats.md",
  mimeType: "text/markdown",
  path: "/workspace/cats.md",
};

// The file the server hands back; mutate to simulate the agent rewriting it.
let onDisk = "# Cats";

function Opener() {
  const { openArtifact } = useViewer();
  return <button onClick={() => openArtifact(wsFile)}>open</button>;
}

function setup(busy: boolean) {
  return render(
    <BusyProvider value={busy}>
      <ViewerProvider>
        <Opener />
        <ArtifactViewer />
      </ViewerProvider>
    </BusyProvider>,
  );
}

describe("ArtifactViewer staleness", () => {
  beforeEach(() => {
    onDisk = "# Cats";
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => new Response(onDisk, { status: 200 })),
    );
  });
  afterEach(() => vi.unstubAllGlobals());

  it("fetches contents for a tab opened from a path", async () => {
    setup(false);
    fireEvent.click(screen.getByText("open"));
    expect(await screen.findByRole("heading", { name: "Cats" })).toBeTruthy();
  });

  it("refetches when the file is re-opened", async () => {
    setup(false);
    fireEvent.click(screen.getByText("open"));
    await screen.findByRole("heading", { name: "Cats" });

    // The agent appends a section behind our back.
    onDisk = "# Cats\n\n# Dogs";
    fireEvent.click(screen.getByText("open"));

    expect(await screen.findByRole("heading", { name: "Dogs" })).toBeTruthy();
  });

  it("survives a re-render while the refetch is in flight", async () => {
    // A turn ending re-renders this tree repeatedly (status strip, message
    // list). If the fetch effect tears down on those renders, the response is
    // dropped and the panel silently keeps stale content.
    let release!: () => void;
    const gate = new Promise<void>((r) => (release = r));
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => {
        await gate;
        return new Response(onDisk, { status: 200 });
      }),
    );

    const view = (busy: boolean, nonce: number) => (
      <BusyProvider value={busy}>
        <ViewerProvider>
          <Opener />
          <ArtifactViewer />
          <span>{nonce}</span>
        </ViewerProvider>
      </BusyProvider>
    );

    const { rerender } = render(view(false, 0));
    fireEvent.click(screen.getByText("open"));

    // Unrelated re-renders land while the request is still open.
    for (let i = 1; i <= 3; i++) rerender(view(false, i));
    release();

    expect(await screen.findByRole("heading", { name: "Cats" })).toBeTruthy();
  });

  it("refetches when a turn ends", async () => {
    const { rerender } = setup(false);
    fireEvent.click(screen.getByText("open"));
    await screen.findByRole("heading", { name: "Cats" });

    const view = (busy: boolean) => (
      <BusyProvider value={busy}>
        <ViewerProvider>
          <Opener />
          <ArtifactViewer />
        </ViewerProvider>
      </BusyProvider>
    );

    // Turn starts, agent rewrites the file, turn ends.
    rerender(view(true));
    onDisk = "# Cats\n\n# Dogs";
    rerender(view(false));

    await waitFor(() =>
      expect(screen.getByRole("heading", { name: "Dogs" })).toBeTruthy(),
    );
  });
});

describe("ArtifactViewer under StrictMode", () => {
  beforeEach(() => {
    onDisk = "# Cats";
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => new Response(onDisk, { status: 200 })),
    );
  });
  afterEach(() => vi.unstubAllGlobals());

  it("loads contents when it mounts with a tab already open", async () => {
    // Mirrors the real app: the right pane shows the side panels until a file
    // is opened, so ArtifactViewer *mounts* with the tab already present.
    // StrictMode double-invokes effects on mount with a cleanup between, so a
    // fetch that records its key before awaiting is cancelled by its own
    // cleanup and skipped on the second pass — leaving the tab blank forever.
    function Pane() {
      const { tabs, openArtifact } = useViewer();
      return (
        <>
          <button onClick={() => openArtifact(wsFile)}>open</button>
          {tabs.length > 0 && <ArtifactViewer />}
        </>
      );
    }

    render(
      <StrictMode>
        <BusyProvider value={false}>
          <ViewerProvider>
            <Pane />
          </ViewerProvider>
        </BusyProvider>
      </StrictMode>,
    );
    fireEvent.click(screen.getByText("open"));
    expect(await screen.findByRole("heading", { name: "Cats" })).toBeTruthy();
  });
});
