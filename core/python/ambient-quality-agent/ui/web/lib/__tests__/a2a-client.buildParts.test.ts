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

import { buildMessageParts } from "@/lib/a2a-client";
import { isTextPart, makeTextPart } from "@/lib/a2a-part";
import type { Part } from "@a2a-js/sdk";

// A 1.0 inline-file part: the `raw` oneof case, with name/mime on the part.
const filePart: Part = {
  content: { $case: "raw", value: "AAAA" as unknown as Buffer },
  metadata: undefined,
  filename: "a.png",
  mediaType: "image/png",
};

describe("buildMessageParts", () => {
  it("omits the text part when text is empty (file with no caption)", () => {
    const parts = buildMessageParts("", [filePart]);
    expect(parts).toEqual([filePart]);
    expect(parts.some(isTextPart)).toBe(false);
  });

  it("omits the text part when text is whitespace only", () => {
    expect(buildMessageParts("   \n", [filePart])).toEqual([filePart]);
  });

  it("keeps the text part (with original text) when it has content", () => {
    const parts = buildMessageParts("hello", []);
    expect(parts).toEqual([makeTextPart("hello")]);
  });

  it("orders text before extra parts", () => {
    const parts = buildMessageParts("caption", [filePart]);
    expect(parts).toEqual([makeTextPart("caption"), filePart]);
  });

  it("returns an empty array when there is nothing to send", () => {
    expect(buildMessageParts("", [])).toEqual([]);
    expect(buildMessageParts("")).toEqual([]);
  });
});
