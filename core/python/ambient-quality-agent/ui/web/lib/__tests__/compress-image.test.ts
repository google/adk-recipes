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
import {
  shouldCompressImage,
  compressImageFile,
  COMPRESS_THRESHOLD_BYTES,
} from "@/lib/compress-image";

// happy-dom has no canvas encoder, so the guard/decision logic is unit-tested
// here; the actual canvas encode path is covered by build + manual QA.
function fileOf(type: string, bytes: number, name = "f"): File {
  return new File([new Uint8Array(bytes)], name, { type });
}

describe("shouldCompressImage", () => {
  it("is true for a large png", () => {
    expect(
      shouldCompressImage(fileOf("image/png", COMPRESS_THRESHOLD_BYTES + 1)),
    ).toBe(true);
  });
  it("is false for a small image", () => {
    expect(shouldCompressImage(fileOf("image/png", 1024))).toBe(false);
  });
  it("is false for non-images and gif/svg", () => {
    expect(shouldCompressImage(fileOf("application/pdf", 9_000_000))).toBe(
      false,
    );
    expect(shouldCompressImage(fileOf("image/gif", 9_000_000))).toBe(false);
    expect(shouldCompressImage(fileOf("image/svg+xml", 9_000_000))).toBe(false);
  });
});

describe("compressImageFile", () => {
  it("returns the original unchanged when not compressible", async () => {
    const f = fileOf("application/pdf", 9_000_000, "doc.pdf");
    await expect(compressImageFile(f)).resolves.toBe(f);
  });
});
