/**
 * Copyright 2026 Google LLC
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     https://www.apache.org/licenses/LICENSE-2.0
 *
 * Unless required by applicable law or agreed to in writing, software
 * distributed under the License is distributed on an "AS IS" BASIS,
 * WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 * See the License for the specific language governing permissions and
 * limitations under the License.
 */

// Uploads go as the original file when the server takes it (its type and size), so the photo keeps its
// full quality and the same photo is always the same bytes. Only a file over the server's limit, or of
// another type, is shrunk in the browser: at most 1600 px on the long side, as JPEG.

/** The image types the server takes (requests.check_image); HEIC and AVIF it converts. */
export const ACCEPT =
  "image/png,image/jpeg,image/webp,image/heic,image/heif,image/avif,.heic,.heif,.avif";

/** The server's limit per image (core/gcs.py MAX_IMAGE_BYTES, checked by requests.check_image). */
export const MAX_UPLOAD_BYTES = 20 * 2 ** 20;

const SERVER_TYPES = [
  "image/png",
  "image/jpeg",
  "image/webp",
  "image/heic",
  "image/heif",
  "image/avif",
];
/** For files the browser gives no type (HEIC on some systems). */
const SERVER_NAMES = /\.(png|jpe?g|webp|heic|heif|avif)$/i;
const MAX_SIDE = 1600;
const QUALITY = 0.9;

/** The file to upload: `file` itself when the server takes it as it is, else `shrinkImage(file)`. */
export async function uploadFile(file: File): Promise<File> {
  const known = file.type
    ? SERVER_TYPES.includes(file.type)
    : SERVER_NAMES.test(file.name);
  return known && file.size <= MAX_UPLOAD_BYTES ? file : shrinkImage(file);
}

/** `file` as a JPEG of at most 1600 px a side, keeping its name; the original when the browser cannot decode it (e.g. HEIC). */
export async function shrinkImage(file: File): Promise<File> {
  let bitmap: ImageBitmap;
  try {
    bitmap = await createImageBitmap(file);
  } catch {
    return file; // The server reads it (or says why not).
  }
  const scale = Math.min(1, MAX_SIDE / Math.max(bitmap.width, bitmap.height));
  const canvas = document.createElement("canvas");
  canvas.width = Math.round(bitmap.width * scale);
  canvas.height = Math.round(bitmap.height * scale);
  const context = canvas.getContext("2d");
  if (context) {
    context.fillStyle = "#fff"; // transparent areas: white, not JPEG black
    context.fillRect(0, 0, canvas.width, canvas.height);
    context.drawImage(bitmap, 0, 0, canvas.width, canvas.height);
  }
  bitmap.close();
  if (!context) return file;
  const blob = await new Promise<Blob | null>((resolve) =>
    canvas.toBlob(resolve, "image/jpeg", QUALITY),
  );
  return blob ? new File([blob], file.name, { type: "image/jpeg" }) : file;
}
