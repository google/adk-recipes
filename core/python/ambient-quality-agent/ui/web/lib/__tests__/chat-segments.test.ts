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

import { appendPartsToSegments } from "@/lib/chat-segments";
import type { Extracted } from "@/lib/horizon-events";
import type { MessageSegment } from "@/components/chat/chat-shell";

describe("appendPartsToSegments", () => {
  it("merges consecutive text parts into one segment", () => {
    const parts: Extracted[] = [
      { kind: "text", text: "Hello, " },
      { kind: "text", text: "world" },
    ];
    const result = appendPartsToSegments([], parts);
    expect(result).toEqual([{ kind: "text", text: "Hello, world" }]);
  });

  it("merges into a trailing text segment in the existing array", () => {
    const existing: MessageSegment[] = [{ kind: "text", text: "Hello, " }];
    const parts: Extracted[] = [{ kind: "text", text: "world" }];
    const result = appendPartsToSegments(existing, parts);
    expect(result).toEqual([{ kind: "text", text: "Hello, world" }]);
  });

  it("returns a new array reference so React.memo sees the change", () => {
    const existing: MessageSegment[] = [];
    const result = appendPartsToSegments(existing, []);
    expect(result).not.toBe(existing);
  });

  it("appends tool parts as standalone segments", () => {
    const parts: Extracted[] = [
      {
        kind: "tool",
        callId: "c1",
        name: "search",
        args: { q: "x" },
        ok: null,
      },
    ];
    const result = appendPartsToSegments([], parts);
    expect(result).toEqual([
      { kind: "tool", callId: "c1", name: "search", args: { q: "x" } },
    ]);
  });

  it("dedupes a function_call surfaced twice by callId (partial + final)", () => {
    // The new-impl converter lifts the call onto a status message from both the
    // partial and the final event, so the same callId arrives twice — across
    // two appends (separate stream events) as well as within one.
    const dup: Extracted = {
      kind: "tool",
      callId: "c1",
      name: "terminal",
      args: { command: "ls" },
      ok: null,
    };
    const once = appendPartsToSegments([], [dup]);
    const twice = appendPartsToSegments(once, [dup]);
    expect(twice.filter((s) => s.kind === "tool")).toHaveLength(1);
  });

  it("does not dedupe distinct tool calls that share a name", () => {
    const parts: Extracted[] = [
      {
        kind: "tool",
        callId: "a",
        name: "terminal",
        args: { command: "echo a" },
        ok: null,
      },
      {
        kind: "tool",
        callId: "b",
        name: "terminal",
        args: { command: "echo b" },
        ok: null,
      },
    ];
    const result = appendPartsToSegments([], parts);
    expect(result.filter((s) => s.kind === "tool")).toHaveLength(2);
  });

  it("attaches a tool_result to the matching tool segment by callId", () => {
    const existing: MessageSegment[] = [
      { kind: "tool", callId: "c1", name: "search", args: { q: "x" } },
    ];
    const parts: Extracted[] = [
      {
        kind: "tool_result",
        callId: "c1",
        name: "search",
        result: { hits: 3 },
      },
    ];
    const result = appendPartsToSegments(existing, parts);
    expect(result).toEqual([
      {
        kind: "tool",
        callId: "c1",
        name: "search",
        args: { q: "x" },
        result: { hits: 3 },
        hasResult: true,
      },
    ]);
  });

  it("falls back to the most recent unmatched tool when callId is missing", () => {
    const existing: MessageSegment[] = [
      { kind: "tool", callId: null, name: "search", args: null },
    ];
    const parts: Extracted[] = [
      { kind: "tool_result", callId: null, name: "search", result: "ok" },
    ];
    const result = appendPartsToSegments(existing, parts);
    expect(result[0]).toMatchObject({ hasResult: true, result: "ok" });
  });

  it("appends file parts", () => {
    const parts: Extracted[] = [
      {
        kind: "file",
        file: { bytes: "AA", mimeType: "image/png", name: "a.png" },
      },
    ];
    const result = appendPartsToSegments([], parts);
    expect(result).toEqual([
      {
        kind: "file",
        file: { bytes: "AA", mimeType: "image/png", name: "a.png" },
      },
    ]);
  });

  it("appends unknown data parts", () => {
    const parts: Extracted[] = [
      { kind: "data", mime: "application/json", data: { x: 1 } },
    ];
    const result = appendPartsToSegments([], parts);
    expect(result).toEqual([
      { kind: "data", mime: "application/json", data: { x: 1 } },
    ]);
  });

  it("dedupes clarify segments by callId (defensive backstop against replayed confirmations)", () => {
    // The HITL gate's twin events are collapsed in mapPart (the raw clarify
    // function_call is suppressed), so duplicates with the same callId only
    // reach here if a confirmation event is replayed twice — dedup it.
    const parts: Extracted[] = [
      {
        kind: "clarify",
        callId: "call-1",
        question: "What would you like to do?",
        choices: ["a", "b"],
      },
      {
        kind: "clarify",
        callId: "call-1",
        question: "What would you like to do?",
        choices: ["a", "b"],
      },
    ];
    const result = appendPartsToSegments([], parts);
    expect(result).toEqual([
      {
        kind: "clarify",
        callId: "call-1",
        question: "What would you like to do?",
        choices: ["a", "b"],
      },
    ]);
  });

  it("allows distinct clarify callIds (two different questions render separately)", () => {
    const parts: Extracted[] = [
      { kind: "clarify", callId: "a", question: "Q1?", choices: [] },
      { kind: "clarify", callId: "b", question: "Q2?", choices: [] },
    ];
    const result = appendPartsToSegments([], parts);
    expect(result.length).toBe(2);
  });

  it("allows a clarify without a callId to fall through (rare replay edge case)", () => {
    const parts: Extracted[] = [
      { kind: "clarify", callId: null, question: "Q?", choices: [] },
      { kind: "clarify", callId: null, question: "Q?", choices: [] },
    ];
    const result = appendPartsToSegments([], parts);
    // No callId means no dedup — surface both rather than collapse different
    // questions that happen to lack an id.
    expect(result.length).toBe(2);
  });

  it("dedupes confirmation segments by callId", () => {
    const parts: Extracted[] = [
      {
        kind: "confirmation",
        callId: "call-2",
        hint: "Run terminal?",
        originalCall: { name: "terminal", args: { cmd: "ls" } },
        payload: null,
      },
      {
        kind: "confirmation",
        callId: "call-2",
        hint: "Run terminal?",
        originalCall: { name: "terminal", args: { cmd: "ls" } },
        payload: null,
      },
    ];
    const result = appendPartsToSegments([], parts);
    expect(result.length).toBe(1);
  });
});
