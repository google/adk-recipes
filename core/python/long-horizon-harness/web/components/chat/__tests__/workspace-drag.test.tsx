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
import { SidebarWorkspaceTree } from "../sidebar-workspace-tree";
import { ViewerProvider } from "../artifact-viewer/viewer-context";
import { FILEREF_DRAG_MIME, parseFileRefDrag } from "../workspace-href";

function stubTree() {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      const nested = url.includes(encodeURIComponent("/workspace/docs"));
      return new Response(
        JSON.stringify({
          entries: nested
            ? [{ name: "plan.md", kind: "file", size: 1 }]
            : [
                { name: "alphabet.md", kind: "file", size: 1 },
                { name: "docs", kind: "dir" },
              ],
        }),
        { status: 200 },
      );
    }),
  );
}

function setup() {
  render(
    <ViewerProvider>
      <SidebarWorkspaceTree
        isExpanded
        workspaceVersion={0}
        optimisticRows={[]}
        onReconciled={() => {}}
      />
    </ViewerProvider>,
  );
}

/** The row's name button; the open-in-new-tab anchor shares its title. */
async function nameButton(name: string) {
  const matches = await screen.findAllByTitle(`Open ${name}`);
  const btn = matches.find((el) => el.tagName === "BUTTON");
  if (!btn) throw new Error(`no draggable row for ${name}`);
  return btn;
}

/** Minimal stand-in: happy-dom has no DataTransfer. */
function dataTransfer() {
  const store: Record<string, string> = {};
  return {
    setData: (t: string, v: string) => {
      store[t] = v;
    },
    getData: (t: string) => store[t] ?? "",
    effectAllowed: "",
    types: [] as string[],
    store,
  };
}

describe("dragging a workspace file", () => {
  beforeEach(stubTree);
  afterEach(() => vi.unstubAllGlobals());

  it("carries the name, size and the path the agent's tools expect", async () => {
    setup();
    const row = await nameButton("alphabet.md");
    const dt = dataTransfer();
    fireEvent.dragStart(row, { dataTransfer: dt });

    expect(parseFileRefDrag(dt.store[FILEREF_DRAG_MIME])).toEqual({
      // /workspace-prefixed: the one form that resolves the same whether or
      // not the chat has a project window set.
      path: "/workspace/alphabet.md",
      name: "alphabet.md",
      size: 1,
    });
  });

  it("sets no text/plain, which would double up with the chip", async () => {
    // A textarea inserts dropped text itself, so carrying text/plain too
    // would type the name alongside showing the chip.
    setup();
    const row = await nameButton("alphabet.md");
    const dt = dataTransfer();
    fireEvent.dragStart(row, { dataTransfer: dt });

    expect(Object.keys(dt.store)).toEqual([FILEREF_DRAG_MIME]);
  });

  it("keeps the folder for a nested file", async () => {
    setup();
    fireEvent.click(await screen.findByText("docs"));
    const row = await nameButton("plan.md");
    const dt = dataTransfer();
    fireEvent.dragStart(row, { dataTransfer: dt });

    expect(parseFileRefDrag(dt.store[FILEREF_DRAG_MIME])?.path).toBe(
      "/workspace/docs/plan.md",
    );
  });
});
