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

import { render, screen, fireEvent, act } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ArtifactViewer } from "../artifact-viewer";
import { ViewerProvider, useViewer } from "../viewer-context";
import { BusyProvider } from "../../busy-context";

// A live workspace file (has `path`) vs a frozen artifact snapshot.
const wsFile = {
  name: "report.md",
  mimeType: "text/markdown",
  bytes: btoa("# Report"),
  path: "/workspace/report.md",
};
const snapshot = {
  name: "spec.md",
  mimeType: "text/markdown",
  bytes: btoa("# Spec"),
};

function Opener({ file }: { file?: object } = {}) {
  const { openArtifact } = useViewer();
  return (
    <>
      <button onClick={() => openArtifact((file ?? wsFile) as never)}>
        open-ws
      </button>
      <button onClick={() => openArtifact(snapshot)}>open-snapshot</button>
    </>
  );
}

function setup(busy = false) {
  return render(
    <BusyProvider value={busy}>
      <ViewerProvider>
        <Opener />
        <ArtifactViewer />
      </ViewerProvider>
    </BusyProvider>,
  );
}

const sourceButton = () => screen.getByRole("button", { name: /^source$/i });

describe("ArtifactViewer editing", () => {
  beforeEach(() => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => new Response("{}", { status: 200 })),
    );
  });

  afterEach(() => {
    vi.useRealTimers();
    vi.unstubAllGlobals();
  });

  it("labels the source toggle 'Source' for a workspace file", () => {
    setup();
    fireEvent.click(screen.getByText("open-ws"));
    expect(sourceButton()).toBeTruthy();
  });

  it("labels it 'Raw' for an artifact snapshot, with no editor", () => {
    setup();
    fireEvent.click(screen.getByText("open-snapshot"));
    expect(screen.getByRole("button", { name: /^raw$/i })).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: /^raw$/i }));
    expect(screen.queryByRole("textbox")).toBeNull();
  });

  it("autosaves a workspace file after the debounce", async () => {
    setup();
    fireEvent.click(screen.getByText("open-ws"));
    fireEvent.click(sourceButton());

    const box = screen.getByRole("textbox") as HTMLTextAreaElement;
    fireEvent.change(box, { target: { value: "# Edited" } });
    expect(screen.getByText(/unsaved/i)).toBeTruthy();

    // No request until the debounce elapses.
    expect(vi.mocked(fetch)).not.toHaveBeenCalled();
    await act(async () => {
      vi.advanceTimersByTime(900);
    });

    const [url, init] = vi.mocked(fetch).mock.calls[0]!;
    expect(String(url)).toContain("/lha/workspace/file");
    expect(String(url)).toContain(encodeURIComponent("/workspace/report.md"));
    expect(init?.method).toBe("PUT");
    expect(JSON.parse(String(init?.body))).toEqual({ content: "# Edited" });
  });

  it("coalesces a typing burst into one request", async () => {
    setup();
    fireEvent.click(screen.getByText("open-ws"));
    fireEvent.click(sourceButton());
    const box = screen.getByRole("textbox");

    for (const v of ["a", "ab", "abc"]) {
      fireEvent.change(box, { target: { value: v } });
      await act(async () => {
        vi.advanceTimersByTime(200);
      });
    }
    await act(async () => {
      vi.advanceTimersByTime(900);
    });

    expect(vi.mocked(fetch)).toHaveBeenCalledTimes(1);
    const [, init] = vi.mocked(fetch).mock.calls[0]!;
    expect(JSON.parse(String(init?.body))).toEqual({ content: "abc" });
  });

  it("locks the editor while the agent owns the turn", () => {
    setup(true);
    fireEvent.click(screen.getByText("open-ws"));
    fireEvent.click(sourceButton());
    const box = screen.getByRole("textbox") as HTMLTextAreaElement;
    expect(box.readOnly).toBe(true);
    expect(screen.getByText(/agent working/i)).toBeTruthy();
  });
});

describe("files the editor must not rewrite", () => {
  beforeEach(() => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => new Response("x", { status: 200 })),
    );
  });
  afterEach(() => vi.unstubAllGlobals());

  it("stays read-only when the bytes are not valid UTF-8", async () => {
    // decodeText replaces invalid sequences with U+FFFD, so a latin-1 file
    // decodes "fine" — and saving that decode back destroys the bytes it
    // could not represent.
    const latin1 = btoa("caf\xe9 au lait");
    render(
      <BusyProvider value={false}>
        <ViewerProvider>
          <Opener
            file={{
              name: "notes.csv",
              mimeType: "text/csv",
              bytes: latin1,
              path: "/workspace/notes.csv",
            }}
          />
          <ArtifactViewer />
        </ViewerProvider>
      </BusyProvider>,
    );
    fireEvent.click(screen.getByText("open-ws"));

    // No Source toggle means no editor, so nothing can save the mangled
    // decode back over the original bytes.
    expect(
      await screen.findByRole("tab", { name: /notes\.csv/i }),
    ).toBeTruthy();
    expect(screen.queryByRole("button", { name: /^source$/i })).toBeNull();
    expect(screen.queryByRole("textbox")).toBeNull();
  });
});
