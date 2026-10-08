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

export const COMPRESS_THRESHOLD_BYTES = 1_500_000;
const MAX_DIMENSION = 2048;
const QUALITY = 0.82;
const SKIP_TYPES = new Set(["image/gif", "image/svg+xml"]);

export function shouldCompressImage(file: File): boolean {
  return (
    file.type.startsWith("image/") &&
    !SKIP_TYPES.has(file.type) &&
    file.size > COMPRESS_THRESHOLD_BYTES
  );
}

// Re-encode an oversized raster image to a smaller JPEG before attaching, so a
// large screenshot doesn't bloat the A2A payload or hit provider limits. Returns
// the original unchanged when not compressible or if compression wouldn't help.
export async function compressImageFile(file: File): Promise<File> {
  if (!shouldCompressImage(file)) return file;
  try {
    const bitmap = await createImageBitmap(file);
    const scale = Math.min(
      1,
      MAX_DIMENSION / Math.max(bitmap.width, bitmap.height),
    );
    const w = Math.round(bitmap.width * scale);
    const h = Math.round(bitmap.height * scale);
    const canvas = document.createElement("canvas");
    canvas.width = w;
    canvas.height = h;
    const ctx = canvas.getContext("2d");
    if (!ctx) return file;
    ctx.drawImage(bitmap, 0, 0, w, h);
    const blob: Blob | null = await new Promise((res) =>
      canvas.toBlob(res, "image/jpeg", QUALITY),
    );
    if (!blob || blob.size >= file.size) return file; // no win → keep original
    const name = file.name.replace(/\.[^./]+$/, "") + ".jpg";
    return new File([blob], name, { type: "image/jpeg" });
  } catch {
    return file; // never block an attach on a compression failure
  }
}
