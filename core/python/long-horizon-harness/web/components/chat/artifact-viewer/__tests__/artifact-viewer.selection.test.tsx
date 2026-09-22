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
import { ArtifactViewer } from "../artifact-viewer";
import { ViewerProvider, useViewer } from "../viewer-context";
import { BusyProvider } from "../../busy-context";

const FILE_TEXT = "alpha\nbravo\ncharlie\n";

const wsFile = {
  name: "cats.md",
  mimeType: "text/markdown",
  bytes: btoa(FILE_TEXT),
  path: "/workspace/cats.md",
};

function Opener() {
  const { openArtifact } = useViewer();
  return <button onClick={() => openArtifact(wsFile)}>open</button>;
}

function setup(onSelectionChange: (s: unknown) => void) {
  return render(
    <BusyProvider value={false}>
      <ViewerProvider onSelectionChange={onSelectionChange}>
        <Opener />
        <ArtifactViewer />
      </ViewerProvider>
    </BusyProvider>,
  );
}

function openSource() {
  fireEvent.click(screen.getByText("open"));
  fireEvent.click(screen.getByRole("button", { name: /^source$/i }));
  return screen.getByRole("textbox") as HTMLTextAreaElement;
}

describe("Source view selection capture", () => {
  beforeEach(() => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => new Response(FILE_TEXT, { status: 200 })),
    );
  });
  afterEach(() => vi.unstubAllGlobals());

  it("captures a highlighted span as a unique anchor", () => {
    const spy = vi.fn();
    setup(spy);
    const box = openSource();
    box.setSelectionRange(6, 11); // "bravo"
    fireEvent.select(box);
    expect(spy).toHaveBeenLastCalledWith({
      path: "/workspace/cats.md",
      anchor: {
        start: 6,
        end: 11,
        snippet: "bravo",
        startLine: 2,
        endLine: 2,
      },
    });
  });

  it("reports null when the selection collapses", () => {
    const spy = vi.fn();
    setup(spy);
    const box = openSource();
    box.setSelectionRange(6, 6);
    fireEvent.select(box);
    expect(spy).toHaveBeenLastCalledWith(null);
  });

  it("reports null on the first keystroke", () => {
    const spy = vi.fn();
    setup(spy);
    const box = openSource();
    box.setSelectionRange(6, 11);
    fireEvent.select(box);
    spy.mockClear();

    fireEvent.change(box, { target: { value: `${FILE_TEXT}x` } });
    expect(spy).toHaveBeenLastCalledWith(null);
  });
});

describe("ViewerProvider selection forwarding", () => {
  it("clears the selection when the conversation changes", () => {
    const spy = vi.fn();
    function Setter() {
      const { setPendingSelection } = useViewer();
      return (
        <button
          onClick={() =>
            setPendingSelection({
              path: "/workspace/cats.md",
              anchor: {
                start: 6,
                end: 11,
                snippet: "bravo",
                startLine: 2,
                endLine: 2,
              },
            })
          }
        >
          set
        </button>
      );
    }
    const view = (key: string) => (
      <ViewerProvider resetKey={key} onSelectionChange={spy}>
        <Setter />
      </ViewerProvider>
    );
    const { rerender } = render(view("chat-a"));
    fireEvent.click(screen.getByText("set"));
    expect(spy).toHaveBeenLastCalledWith(
      expect.objectContaining({ path: "/workspace/cats.md" }),
    );
    spy.mockClear();

    rerender(view("chat-b"));
    expect(spy).toHaveBeenLastCalledWith(null);
  });
});
