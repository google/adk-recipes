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
import type { Part } from "@a2a-js/sdk";

import { extractParts, isFinal, mapPart } from "@/lib/horizon-events";

import fileFixture from "@/lib/__fixtures__/events/file-attachment.json";
import fnCallFixture from "@/lib/__fixtures__/events/function-call-generic.json";
import fnRespFixture from "@/lib/__fixtures__/events/function-response-dropped.json";
import textStreamFixture from "@/lib/__fixtures__/events/text-stream.json";
import userEchoFixture from "@/lib/__fixtures__/events/user-echo-status-update.json";

describe("extractParts", () => {
  it("extracts text from artifactUpdate deltas", () => {
    const [first] = textStreamFixture.events;
    expect(extractParts(first)).toEqual([{ kind: "text", text: "Hello, " }]);
  });

  it("skips user-authored statusUpdate echoes (role=user)", () => {
    expect(extractParts(userEchoFixture.event)).toEqual([]);
  });

  it("extracts generic function_call DataParts as tool segments", () => {
    expect(extractParts(fnCallFixture.event)).toEqual([
      {
        kind: "tool",
        callId: null,
        name: "web_search",
        args: { query: "anthropic adk" },
        ok: null,
      },
    ]);
  });

  it("surfaces function_response DataParts as tool_result", () => {
    expect(extractParts(fnRespFixture.event)).toEqual([
      {
        kind: "tool_result",
        callId: null,
        name: "web_search",
        result: { results: ["doc1", "doc2"] },
      },
    ]);
  });

  it("extracts file parts from artifactUpdate events", () => {
    const parts = extractParts(fileFixture.event);
    expect(parts).toHaveLength(1);
    expect(parts[0]).toMatchObject({
      kind: "file",
      file: { mimeType: "image/png", name: "tiny.png" },
    });
  });

  it("returns both text and file parts from artifactUpdate events", () => {
    const event = {
      payload: {
        $case: "artifactUpdate",
        value: {
          taskId: "task-1",
          contextId: "ctx-1",
          artifact: {
            parts: [
              {
                content: { $case: "text", value: "streamed text" },
                metadata: undefined,
                filename: "",
                mediaType: "",
              },
              {
                content: { $case: "raw", value: "AAAA" },
                metadata: undefined,
                filename: "a.png",
                mediaType: "image/png",
              },
            ],
          },
          append: false,
          lastChunk: true,
        },
      },
    };
    const parts = extractParts(event);
    expect(parts).toEqual([
      { kind: "text", text: "streamed text" },
      expect.objectContaining({ kind: "file" }),
    ]);
  });

  it("skips a lha_ge_label text part (the 📎 artifact link, rendered inline instead)", () => {
    // The 📎 saved-artifact link is GE-only: the web renders the artifact inline
    // from its FilePart, so mapPart drops the marked link to avoid a duplicate
    // (see horizon/a2a/executor.py _surface_artifact_links).
    const event = {
      payload: {
        $case: "statusUpdate",
        value: {
          taskId: "task-1",
          contextId: "ctx-1",
          status: {
            state: 2,
            message: {
              role: 2,
              parts: [
                {
                  content: {
                    $case: "text",
                    value: "\n\n📎 [report.html](https://example/signed)\n\n",
                  },
                  metadata: { lha_ge_label: true },
                  filename: "",
                  mediaType: "",
                },
              ],
            },
          },
        },
      },
    };
    expect(extractParts(event)).toEqual([]);
  });
});

describe("mapPart", () => {
  it("returns null for Parts with no recognized content case", () => {
    // No content.$case — hits none of isTextPart/isFilePart/isDataPart.
    expect(
      mapPart({ kind: "unknown" } as unknown as Parameters<typeof mapPart>[0]),
    ).toBeNull();
  });

  it("forwards raw-bytes file Parts through", () => {
    const result = mapPart({
      content: { $case: "raw", value: "AAAA" as unknown as Buffer },
      metadata: undefined,
      filename: "x.png",
      mediaType: "image/png",
    } as Part);
    expect(result).toEqual({
      kind: "file",
      file: {
        bytes: "AAAA",
        uri: undefined,
        mimeType: "image/png",
        name: "x.png",
      },
    });
  });

  it("falls back to a 'data' kind for unrecognized DataParts", () => {
    const result = mapPart({
      content: { $case: "data", value: { hello: "world" } },
      metadata: { mimeType: "application/json" },
      filename: "",
      mediaType: "",
    } as Part);
    expect(result).toEqual({
      kind: "data",
      mime: "application/json",
      data: { hello: "world" },
    });
  });

  it("strips the GE horizon__ prefix from tool call names", () => {
    // The backend prefixes flat names so GE's <agent>__<action> timeline filter
    // shows them (horizon/a2a/executor.py); the web renders the flat label.
    expect(
      mapPart({
        content: {
          $case: "data",
          value: { id: "c1", name: "horizon__terminal", args: { cmd: "ls" } },
        },
        metadata: { adk_type: "function_call" },
        filename: "",
        mediaType: "",
      } as Part),
    ).toEqual({
      kind: "tool",
      callId: "c1",
      name: "terminal",
      args: { cmd: "ls" },
      ok: null,
    });
  });

  it("matches the confirmation path even when the name is GE-prefixed", () => {
    // adk_request_confirmation keeps its flat name today, but the strip must be
    // applied before the special-case match so it stays robust if that changes.
    const result = mapPart({
      content: {
        $case: "data",
        value: {
          id: "c1",
          name: "horizon__adk_request_confirmation",
          response: { confirmed: true },
        },
      },
      metadata: { adk_type: "function_response" },
      filename: "",
      mediaType: "",
    } as Part);
    expect(result).toEqual({
      kind: "hitl_result",
      callId: "c1",
      confirmed: true,
      text: null,
    });
  });
});

describe("isFinal", () => {
  it("is true for any message event", () => {
    expect(
      isFinal({
        payload: {
          $case: "message",
          value: {
            messageId: "m1",
            contextId: "c1",
            taskId: "t1",
            role: 2,
            parts: [],
            metadata: undefined,
            extensions: [],
            referenceTaskIds: [],
          },
        },
      }),
    ).toBe(true);
  });

  it("is true for statusUpdate with terminal state (COMPLETED=3)", () => {
    expect(isFinal(textStreamFixture.events[2])).toBe(true);
  });

  it("is false for statusUpdate with non-terminal state (WORKING=2)", () => {
    expect(
      isFinal({
        payload: {
          $case: "statusUpdate",
          value: { status: { state: 2 } },
        },
      }),
    ).toBe(false);
  });

  it("is true for task events in terminal state", () => {
    // COMPLETED=3, FAILED=4, CANCELED=5, REJECTED=7
    for (const state of [3, 4, 5, 7]) {
      expect(
        isFinal({ payload: { $case: "task", value: { status: { state } } } }),
      ).toBe(true);
    }
  });

  it("is false for task events in non-terminal state (WORKING=2)", () => {
    expect(
      isFinal({ payload: { $case: "task", value: { status: { state: 2 } } } }),
    ).toBe(false);
  });
});
