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

import { render, screen, waitFor, fireEvent } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { Markdown } from "../markdown";
import { ArtifactViewer } from "../artifact-viewer/artifact-viewer";
import { ViewerProvider } from "../artifact-viewer/viewer-context";
import { __resetWorkspaceFiles } from "@/lib/workspace-files";

// One file at the root, one nested — the walk goes two levels deep.
function stubTree() {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes("/lha/workspace/tree")) {
        const body = url.includes(encodeURIComponent("/workspace/docs"))
          ? { entries: [{ name: "plan.md", kind: "file" }] }
          : {
              entries: [
                { name: "cats.md", kind: "file" },
                { name: "docs", kind: "dir" },
              ],
            };
        return new Response(JSON.stringify(body), { status: 200 });
      }
      return new Response("", { status: 200 });
    }),
  );
}

function setup(text: string) {
  return render(
    <ViewerProvider>
      <Markdown text={text} />
      <ArtifactViewer />
    </ViewerProvider>,
  );
}

describe("backticked filename mentions", () => {
  beforeEach(() => {
    __resetWorkspaceFiles();
    stubTree();
  });
  afterEach(() => vi.unstubAllGlobals());

  it("linkifies a filename that exists in the workspace", async () => {
    setup("I have created `cats.md` in your workspace.");
    const link = await screen.findByRole("button", { name: "cats.md" });
    fireEvent.click(link);
    expect(screen.getByRole("tab", { name: /cats\.md/i })).toBeTruthy();
  });

  it("linkifies a nested file found by the two-level walk", async () => {
    setup("See `docs/plan.md` for the outline.");
    expect(
      await screen.findByRole("button", { name: "docs/plan.md" }),
    ).toBeTruthy();
  });

  it("leaves a filename that does not exist as plain code", async () => {
    setup("Check `ghost.md` for details.");
    // Wait for the listing so this isn't just passing on a pending fetch.
    await waitFor(() => expect(vi.mocked(fetch)).toHaveBeenCalled());
    await waitFor(() =>
      expect(screen.queryByRole("button", { name: "ghost.md" })).toBeNull(),
    );
    expect(screen.getByText("ghost.md").tagName).toBe("CODE");
  });

  it("leaves a backticked shell command alone", async () => {
    setup("Run `npm run build` first.");
    await waitFor(() => expect(vi.mocked(fetch)).toHaveBeenCalled());
    expect(screen.queryByRole("button", { name: /npm run build/ })).toBeNull();
  });
});
