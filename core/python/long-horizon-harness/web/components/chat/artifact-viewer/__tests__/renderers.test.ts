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

import { describe, expect, it } from "vitest";
import { decodeText, pickRenderer, supportsPreview } from "../renderers";

describe("pickRenderer", () => {
  it("resolves by mime type", () => {
    expect(pickRenderer("text/markdown", "x")).toBe("markdown");
    expect(pickRenderer("image/png", "x")).toBe("image");
    expect(pickRenderer("audio/mpeg", "x")).toBe("audio");
    expect(pickRenderer("text/html", "x")).toBe("html");
    expect(pickRenderer("application/json", "x")).toBe("json");
    expect(pickRenderer("text/plain", "x")).toBe("code");
    expect(pickRenderer("application/octet-stream", "x")).toBe("download");
  });

  it("falls back to the filename extension when mime is generic", () => {
    expect(pickRenderer("application/octet-stream", "readme.md")).toBe(
      "markdown",
    );
    expect(pickRenderer("application/octet-stream", "main.py")).toBe("code");
    expect(pickRenderer("application/octet-stream", "page.html")).toBe("html");
    expect(pickRenderer("application/octet-stream", "data.json")).toBe("json");
    expect(pickRenderer("", "photo.jpg")).toBe("image");
    expect(pickRenderer("", "archive.zip")).toBe("download");
  });

  it("only markdown and html expose a preview/raw toggle", () => {
    expect(supportsPreview("markdown")).toBe(true);
    expect(supportsPreview("html")).toBe(true);
    expect(supportsPreview("code")).toBe(false);
    expect(supportsPreview("image")).toBe(false);
    expect(supportsPreview("json")).toBe(false);
    expect(supportsPreview("download")).toBe(false);
  });
});

describe("decodeText", () => {
  it("decodes base64 bytes to utf-8 text", () => {
    const b64 = btoa("# hello");
    expect(
      decodeText({
        id: "a",
        revision: 0,
        name: "a.md",
        mimeType: "text/markdown",
        bytes: b64,
      }),
    ).toBe("# hello");
  });

  it("returns null when there are no bytes", () => {
    expect(
      decodeText({
        id: "a",
        revision: 0,
        name: "a.md",
        mimeType: "text/markdown",
        url: "http://x",
      }),
    ).toBeNull();
  });
});

describe("pdf", () => {
  it("routes a pdf to its own renderer, by mime or extension", () => {
    expect(pickRenderer("application/pdf", "report.pdf")).toBe("pdf");
    // Opened from the tree, where only the filename is known.
    expect(pickRenderer("", "report.pdf")).toBe("pdf");
  });

  it("has no source toggle", () => {
    expect(supportsPreview("pdf")).toBe(false);
  });
});
