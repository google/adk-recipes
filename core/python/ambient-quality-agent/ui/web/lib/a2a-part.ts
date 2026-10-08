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

// The only module that knows the A2A wire shape. Parts use a ts-proto tagged
// union (`content.$case`); stream events wrap their payload the same way.
// Everything else in the web tree reads through these accessors.

import { Role } from "@a2a-js/sdk";
import type { Message, Part } from "@a2a-js/sdk";

type PartCase = "text" | "raw" | "url" | "data";

function caseOf(p: unknown): PartCase | null {
  const c = (p as { content?: { $case?: string } } | null)?.content?.$case;
  return (c as PartCase) ?? null;
}

function valueOf(p: unknown): unknown {
  return (p as { content?: { value?: unknown } } | null)?.content?.value;
}

export function isTextPart(p: Part): boolean {
  return caseOf(p) === "text";
}

export function partText(p: Part): string | null {
  return caseOf(p) === "text" ? String(valueOf(p)) : null;
}

export function isDataPart(p: Part): boolean {
  return caseOf(p) === "data";
}

export function dataPartDict(p: Part): Record<string, unknown> | null {
  if (caseOf(p) !== "data") return null;
  const v = valueOf(p);
  return v && typeof v === "object" ? (v as Record<string, unknown>) : null;
}

/** The data payload is a protobuf Value, so a scalar or array is also valid. */
export function dataPartValue(p: Part): unknown {
  return caseOf(p) === "data" ? valueOf(p) : null;
}

// A file part carries either inline bytes or a URL, never both.
export function isFilePart(p: Part): boolean {
  const c = caseOf(p);
  return c === "raw" || c === "url";
}

export function fileUrl(p: Part): string | null {
  return caseOf(p) === "url" ? String(valueOf(p)) : null;
}

// `raw` is typed Buffer by the SDK; Buffer must not escape this module.
// Chunked so a multi-MB image doesn't build one giant intermediate string.
function bytesToBase64(bytes: Uint8Array): string {
  const CHUNK = 0x8000;
  let bin = "";
  for (let i = 0; i < bytes.length; i += CHUNK) {
    bin += String.fromCharCode(...bytes.subarray(i, i + CHUNK));
  }
  return btoa(bin);
}

// Parts are stable objects within a cached Task, and history rebuilds re-map
// every part on each refetch — without this a few MB of images get re-encoded
// on the main thread every time.
const encodedBytes = new WeakMap<object, string>();

export function fileBytes(p: Part): string | null {
  if (caseOf(p) !== "raw") return null;
  const v = valueOf(p);
  if (v == null) return null;
  if (typeof v === "string") return v;
  const cached = encodedBytes.get(v as object);
  if (cached !== undefined) return cached;
  if (v instanceof Uint8Array) {
    const b64 = bytesToBase64(v);
    encodedBytes.set(v, b64);
    return b64;
  }
  if (v instanceof ArrayBuffer) {
    const b64 = bytesToBase64(new Uint8Array(v));
    encodedBytes.set(v, b64);
    return b64;
  }
  // Anything else would coerce to an empty array, and an empty string reads as
  // "no content" upstream — the attachment would vanish with no trace.
  console.error("a2a-part: unrecognised raw part value", v);
  return null;
}

// All Part fields are required; there is no partial constructor.
export function makeTextPart(text: string): Part {
  return {
    content: { $case: "text", value: text },
    metadata: undefined,
    filename: "",
    mediaType: "",
  };
}

export function makeDataPart(
  data: Record<string, unknown>,
  metadata?: Record<string, unknown>,
): Part {
  return {
    content: { $case: "data", value: data },
    metadata,
    filename: "",
    mediaType: "",
  };
}

function base64ToBytes(b64: string): Uint8Array {
  const bin = atob(b64);
  const out = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
  return out;
}

/** Takes base64; the `raw` field holds decoded bytes and is re-encoded on the wire. */
export function makeFileBytesPart(
  bytes: string,
  opts: { filename?: string; mediaType?: string } = {},
): Part {
  return {
    content: {
      $case: "raw",
      value: base64ToBytes(bytes) as unknown as Buffer,
    },
    metadata: undefined,
    filename: opts.filename ?? "",
    mediaType: opts.mediaType ?? "",
  };
}

export function makeFileUrlPart(
  url: string,
  opts: { filename?: string; mediaType?: string } = {},
): Part {
  return {
    content: { $case: "url", value: url },
    metadata: undefined,
    filename: opts.filename ?? "",
    mediaType: opts.mediaType ?? "",
  };
}

// Accepts both the numeric enum and the string form from persisted task history.
export function isUserRole(role: unknown): boolean {
  return role === Role.ROLE_USER || role === "user";
}

/** Filename from the part's top-level field, not from a nested file object. */
export function partFilename(p: Part): string {
  return p?.filename ?? "";
}

export function partMediaType(p: Part): string {
  return p?.mediaType ?? "";
}

// Every field is required; there is no partial constructor.
export function makeUserMessage(args: {
  messageId: string;
  contextId: string;
  parts: Part[];
  taskId?: string;
}): Message {
  return {
    messageId: args.messageId,
    contextId: args.contextId,
    taskId: args.taskId ?? "",
    role: Role.ROLE_USER,
    parts: args.parts,
    metadata: undefined,
    extensions: [],
    referenceTaskIds: [],
  };
}

export type StreamCase = "task" | "message" | "statusUpdate" | "artifactUpdate";

// Stream events arrive loosely typed, so a stale `.kind` read compiles fine and
// silently matches nothing. These accessors are the only safe way to branch.
export function streamCase(e: unknown): StreamCase | null {
  const c = (e as { payload?: { $case?: string } } | null)?.payload?.$case;
  return (c as StreamCase) ?? null;
}

export function streamValue(e: unknown): unknown {
  const p = (e as { payload?: { value?: unknown } } | null)?.payload;
  return p && "value" in p ? p.value : null;
}
