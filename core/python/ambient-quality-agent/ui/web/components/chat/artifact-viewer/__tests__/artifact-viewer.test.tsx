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
import { describe, expect, it } from "vitest";
import { ArtifactViewer } from "../artifact-viewer";
import { ViewerProvider, useViewer } from "../viewer-context";

const md = {
  name: "spec.md",
  mimeType: "text/markdown",
  bytes: btoa("# Hello World"),
};
const png = { name: "plot.png", mimeType: "image/png", bytes: btoa("img") };

function Opener() {
  const { openArtifact } = useViewer();
  return (
    <>
      <button onClick={() => openArtifact(md)}>open-md</button>
      <button onClick={() => openArtifact(png)}>open-png</button>
    </>
  );
}

function setup() {
  return render(
    <ViewerProvider>
      <Opener />
      <ArtifactViewer />
    </ViewerProvider>,
  );
}

describe("ArtifactViewer", () => {
  it("shows an empty state with no open tabs", () => {
    setup();
    expect(screen.getByText(/no file open/i)).toBeTruthy();
  });

  it("opens a markdown file as a tab and renders Preview", () => {
    setup();
    fireEvent.click(screen.getByText("open-md"));
    // tab label
    expect(screen.getByRole("tab", { name: /spec\.md/i })).toBeTruthy();
    // preview renders the heading
    expect(screen.getByRole("heading", { name: "Hello World" })).toBeTruthy();
  });

  it("toggles Preview/Raw for markdown", () => {
    setup();
    fireEvent.click(screen.getByText("open-md"));
    expect(screen.getByRole("heading", { name: "Hello World" })).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: /^raw$/i }));
    // raw source: no rendered heading
    expect(screen.queryByRole("heading", { name: "Hello World" })).toBeNull();
  });

  it("activates a second tab and renders an image", () => {
    setup();
    fireEvent.click(screen.getByText("open-md"));
    fireEvent.click(screen.getByText("open-png"));
    expect(screen.getByAltText("plot.png")).toBeTruthy();
  });

  it("renders a download link with the artifact's data URL", () => {
    setup();
    fireEvent.click(screen.getByText("open-md"));
    const link = screen.getByLabelText("Download") as HTMLAnchorElement;
    expect(link.getAttribute("download")).toBe("spec.md");
    expect(link.getAttribute("href")).toBe(
      `data:text/markdown;base64,${md.bytes}`,
    );
  });

  it("closes a tab", () => {
    setup();
    fireEvent.click(screen.getByText("open-md"));
    expect(screen.getByRole("tab", { name: /spec\.md/i })).toBeTruthy();
    fireEvent.click(screen.getByLabelText("Close spec.md"));
    expect(screen.queryByRole("tab", { name: /spec\.md/i })).toBeNull();
    expect(screen.getByText(/no file open/i)).toBeTruthy();
  });
});
