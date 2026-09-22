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
import { AutoOpenNewFile } from "../auto-open-new-file";
import { ArtifactViewer } from "../artifact-viewer";
import { ViewerProvider, useViewer } from "../viewer-context";
import { BusyProvider } from "../../busy-context";

// The workspace the fake backend reports, mutated between turns.
let tree: { name: string; kind: string; mtime: number }[] = [];
let missing = new Set<string>();

function stubBackend() {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes("/lha/workspace/tree")) {
        return new Response(JSON.stringify({ entries: tree }), { status: 200 });
      }
      if (url.includes("/lha/workspace/download")) {
        const path = decodeURIComponent(
          new URL(url, "http://x").searchParams.get("path") ?? "",
        );
        if (missing.has(path)) return new Response("", { status: 404 });
        return new Response("# Body", { status: 200 });
      }
      return new Response("", { status: 200 });
    }),
  );
}

function file(name: string, mtime: number) {
  return { name, kind: "file", mtime };
}

function Opener({ path }: { path: string }) {
  const { openArtifact, tabs } = useViewer();
  return tabs.length === 0 ? (
    <button
      onClick={() =>
        openArtifact({
          name: path.split("/").pop()!,
          mimeType: "text/markdown",
          path,
        })
      }
    >
      preopen
    </button>
  ) : null;
}

function Harness({ busy, preopen }: { busy: boolean; preopen?: string }) {
  return (
    <BusyProvider value={busy}>
      <ViewerProvider>
        <AutoOpenNewFile />
        {preopen && <Opener path={preopen} />}
        <ArtifactViewer />
      </ViewerProvider>
    </BusyProvider>
  );
}

describe("AutoOpenNewFile", () => {
  beforeEach(() => {
    tree = [];
    missing = new Set();
    stubBackend();
  });
  afterEach(() => vi.unstubAllGlobals());

  /**
   * Settle the initial listing before a turn, so "nothing happened" cases are
   * genuinely waiting for the diff rather than passing before it resolves.
   */
  async function settled(el: React.ReactElement) {
    const r = render(el);
    await waitFor(() =>
      expect(vi.mocked(fetch).mock.calls.length).toBeGreaterThan(0),
    );
    return r;
  }

  it("opens a file the turn created", async () => {
    const { rerender } = await settled(<Harness busy={false} />);

    rerender(<Harness busy={true} />);
    tree = [file("animals.md", 100)];
    rerender(<Harness busy={false} />);

    expect(
      await screen.findByRole("tab", { name: /animals\.md/i }),
    ).toBeTruthy();
  });

  it("opens every file the turn created, newest in front", async () => {
    const { rerender } = await settled(<Harness busy={false} />);

    rerender(<Harness busy={true} />);
    tree = [file("first.md", 100), file("second.md", 200)];
    rerender(<Harness busy={false} />);

    expect(
      await screen.findByRole("tab", { name: /second\.md/i }),
    ).toBeTruthy();
    expect(screen.getByRole("tab", { name: /first\.md/i })).toBeTruthy();
    // Opened oldest-first, so the newest is the one selected.
    expect(
      screen
        .getByRole("tab", { name: /second\.md/i })
        .getAttribute("aria-selected"),
    ).toBe("true");
  });

  it("adds a new file alongside one you already have open", async () => {
    tree = [file("other.md", 50)];
    const { rerender } = await settled(
      <Harness busy={false} preopen="/workspace/other.md" />,
    );
    fireEvent.click(await screen.findByText("preopen"));

    rerender(<Harness busy={true} preopen="/workspace/other.md" />);
    tree = [file("other.md", 50), file("report.md", 200)];
    rerender(<Harness busy={false} preopen="/workspace/other.md" />);

    expect(
      await screen.findByRole("tab", { name: /report\.md/i }),
    ).toBeTruthy();
    // The tab you had stays; it is just no longer in front.
    expect(screen.getByRole("tab", { name: /other\.md/i })).toBeTruthy();
  });

  it("never opens the agent's own working files", async () => {
    // plan.md is usually written last, so without this it would sit in front
    // of the thing that was actually asked for.
    const { rerender } = await settled(<Harness busy={false} />);

    rerender(<Harness busy={true} />);
    tree = [file("report.md", 100), file("plan.md", 300)];
    rerender(<Harness busy={false} />);

    expect(
      await screen.findByRole("tab", { name: /report\.md/i }),
    ).toBeTruthy();
    expect(screen.queryByRole("tab", { name: /plan\.md/i })).toBeNull();
  });

  it("does nothing when a turn only touches working files", async () => {
    tree = [file("report.md", 100)];
    const { rerender } = await settled(<Harness busy={false} />);

    rerender(<Harness busy={true} />);
    tree = [file("report.md", 100), file("plan.md", 300)];
    rerender(<Harness busy={false} />);

    await waitFor(() =>
      expect(vi.mocked(fetch).mock.calls.length).toBeGreaterThan(1),
    );
    expect(screen.getByText(/no file open/i)).toBeTruthy();
  });

  it("swaps the tab over on a rename", async () => {
    // `mv` produces no write tool call: the old file's tab 404s and closes,
    // and the new name opens as a file the turn created.
    tree = [file("oldname.md", 50)];
    const { rerender } = await settled(
      <Harness busy={false} preopen="/workspace/oldname.md" />,
    );
    fireEvent.click(await screen.findByText("preopen"));

    rerender(<Harness busy={true} preopen="/workspace/oldname.md" />);
    tree = [file("newname.md", 300)];
    missing.add("/workspace/oldname.md");
    rerender(<Harness busy={false} preopen="/workspace/oldname.md" />);

    expect(
      await screen.findByRole("tab", { name: /newname\.md/i }),
    ).toBeTruthy();
    await waitFor(() =>
      expect(screen.queryByRole("tab", { name: /oldname\.md/i })).toBeNull(),
    );
  });

  it("does nothing when the turn created no files", async () => {
    tree = [file("existing.md", 10)];
    const { rerender } = await settled(<Harness busy={false} />);

    rerender(<Harness busy={true} />);
    rerender(<Harness busy={false} />);

    await waitFor(() =>
      expect(vi.mocked(fetch).mock.calls.length).toBeGreaterThan(1),
    );
    expect(screen.getByText(/no file open/i)).toBeTruthy();
  });
});
