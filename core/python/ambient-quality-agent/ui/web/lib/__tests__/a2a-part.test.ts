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
  isTextPart,
  partText,
  isDataPart,
  dataPartDict,
  isFilePart,
  fileBytes,
  fileUrl,
  makeTextPart,
  makeDataPart,
  dataPartValue,
  makeFileBytesPart,
  makeUserMessage,
  partFilename,
  partMediaType,
  streamCase,
  streamValue,
} from "../a2a-part";
import type { Part } from "@a2a-js/sdk";

// 1.0 Part requires metadata/filename/mediaType beside the content oneof.
function part(content: Part["content"], extra: Partial<Part> = {}): Part {
  return {
    content,
    metadata: undefined,
    filename: "",
    mediaType: "",
    ...extra,
  };
}

describe("a2a-part: part accessors", () => {
  it("reads a 1.0 text part", () => {
    const p = part({ $case: "text", value: "hi" });
    expect(isTextPart(p)).toBe(true);
    expect(partText(p)).toBe("hi");
    expect(isDataPart(p)).toBe(false);
  });

  it("reads a 1.0 data part", () => {
    const p = part({ $case: "data", value: { adk_type: "tool" } });
    expect(isDataPart(p)).toBe(true);
    expect(dataPartDict(p)).toEqual({ adk_type: "tool" });
    expect(partText(p)).toBeNull();
  });

  it("reads a file url part", () => {
    const p = part({ $case: "url", value: "https://x/y.png" });
    expect(isFilePart(p)).toBe(true);
    expect(fileUrl(p)).toBe("https://x/y.png");
    expect(fileBytes(p)).toBeNull();
  });

  it("returns base64 for raw bytes and never leaks a Buffer", () => {
    const p = part({
      $case: "raw",
      value: new Uint8Array([104, 105]) as unknown as Buffer,
    });
    expect(isFilePart(p)).toBe(true);
    const b = fileBytes(p);
    expect(typeof b).toBe("string");
    expect(b).toBe("aGk=");
  });

  it("passes through raw bytes already encoded as base64 string", () => {
    const p = part({ $case: "raw", value: "aGk=" as unknown as Buffer });
    expect(fileBytes(p)).toBe("aGk=");
  });

  it("exposes filename and mediaType from the part itself", () => {
    const p = part(
      { $case: "url", value: "https://x/y.png" },
      {
        filename: "y.png",
        mediaType: "image/png",
      },
    );
    expect(partFilename(p)).toBe("y.png");
    expect(partMediaType(p)).toBe("image/png");
  });

  it("tolerates an absent content oneof", () => {
    const empty = part(undefined);
    expect(isTextPart(empty)).toBe(false);
    expect(partText(empty)).toBeNull();
    expect(dataPartDict(empty)).toBeNull();
    expect(isFilePart(empty)).toBe(false);
  });

  it("builds parts and a fully-populated user message", () => {
    expect(makeTextPart("yo")).toEqual(part({ $case: "text", value: "yo" }));
    expect(makeDataPart({ a: 1 })).toEqual(
      part({ $case: "data", value: { a: 1 } }),
    );
    const m = makeUserMessage({
      messageId: "m1",
      contextId: "c1",
      parts: [makeTextPart("yo")],
    });
    // Proto Message has no create()/fromPartial() — every field must be present.
    expect(m.messageId).toBe("m1");
    expect(m.contextId).toBe("c1");
    expect(m.taskId).toBe("");
    expect(m.extensions).toEqual([]);
    expect(m.referenceTaskIds).toEqual([]);
    expect(m.parts).toHaveLength(1);
  });
});

// A 1.0 StreamResponse is { payload: { $case, value } }. v0.3 used a flat
// `kind` discriminator, and the update cases changed kebab -> camel. Reading
// `.kind` against 1.0 silently yields undefined, so this is deliberately
// covered: the type system cannot catch it (events arrive as `unknown`).
describe("a2a-part: stream event accessors", () => {
  it("unwraps a message payload", () => {
    const e = {
      payload: { $case: "message" as const, value: { messageId: "m1" } },
    };
    expect(streamCase(e)).toBe("message");
    expect(streamValue(e)).toEqual({ messageId: "m1" });
  });

  it("unwraps a task payload", () => {
    const e = { payload: { $case: "task" as const, value: { id: "t1" } } };
    expect(streamCase(e)).toBe("task");
    expect(streamValue(e)).toEqual({ id: "t1" });
  });

  it("uses camelCase update cases, not v0.3 kebab-case", () => {
    const s = {
      payload: { $case: "statusUpdate" as const, value: { taskId: "t1" } },
    };
    const a = {
      payload: { $case: "artifactUpdate" as const, value: { taskId: "t1" } },
    };
    expect(streamCase(s)).toBe("statusUpdate");
    expect(streamCase(a)).toBe("artifactUpdate");
  });

  it("returns null for a v0.3-shaped event so stale readers fail loudly", () => {
    expect(streamCase({ kind: "message", parts: [] })).toBeNull();
    expect(streamValue({ kind: "message", parts: [] })).toBeNull();
  });

  it("tolerates junk", () => {
    expect(streamCase(null)).toBeNull();
    expect(streamCase(undefined)).toBeNull();
    expect(streamCase({})).toBeNull();
    expect(streamValue({})).toBeNull();
  });
});

// Guards the outbound path against double-encoding: makeFileBytesPart takes a
// base64 string, but the `raw` oneof holds real bytes and the SDK base64-encodes
// it again on serialisation. Mocked SDK tests cannot catch this.
describe("a2a-part: outbound file bytes round-trip through the real SDK", () => {
  it("serialises to the original base64, not base64-of-base64", async () => {
    const { Part } = await import("@a2a-js/sdk");
    const b64 = btoa("hello");
    const part = makeFileBytesPart(b64, {
      filename: "a.txt",
      mediaType: "text/plain",
    });
    const wire = Part.toJSON(part) as { raw?: string };
    expect(wire.raw).toBe(b64);
  });
});

describe("a2a-part: scalar data payloads and byte-encoding cache", () => {
  it("preserves non-object data payloads that dataPartDict drops", () => {
    // The wire type is a protobuf Value, so scalars and arrays are legal.
    const scalar = part({ $case: "data", value: "just-a-string" });
    expect(dataPartDict(scalar)).toBeNull();
    expect(dataPartValue(scalar)).toBe("just-a-string");

    const arr = part({ $case: "data", value: [1, 2, 3] });
    expect(dataPartValue(arr)).toEqual([1, 2, 3]);
  });

  it("returns the identical encoded string for repeated reads of one part", () => {
    const bytes = new Uint8Array([104, 105]);
    const p = part({ $case: "raw", value: bytes as unknown as Buffer });
    const a = fileBytes(p);
    const b = fileBytes(p);
    expect(a).toBe("aGk=");
    expect(b).toBe(a);
  });
});
