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

import { extractParts, mapPart } from "@/lib/horizon-events";

import clarifyFixture from "@/lib/__fixtures__/events/tool-clarify.json";
import clarifyFreeformFixture from "@/lib/__fixtures__/events/tool-clarify-freeform.json";
import loadSkillFixture from "@/lib/__fixtures__/events/tool-load-skill.json";
import confClarifyFixture from "@/lib/__fixtures__/events/tool-confirmation-clarify.json";
import confYesNoFixture from "@/lib/__fixtures__/events/tool-confirmation-yesno.json";

describe("mapPart — clarify special case", () => {
  // The model's raw clarify function_call is suppressed: the interactive card
  // is rendered from the paired adk_request_confirmation, which carries the
  // callId ADK awaits in the continuation. Rendering both produced two cards
  // (and the raw one's callId can't resume the agent).
  it("suppresses the raw clarify function_call (the card comes from the confirmation)", () => {
    const parts = extractParts(clarifyFixture.event);
    expect(parts).toEqual([]);
  });

  it("suppresses raw clarify calls with no choices (free-form) too", () => {
    const parts = extractParts(clarifyFreeformFixture.event);
    expect(parts).toEqual([]);
  });
});

describe("mapPart — load_skill special case", () => {
  it("recognizes load_skill and emits a tool segment with a meta flag", () => {
    const parts = extractParts(loadSkillFixture.event);
    // load_skill stays a tool, but mapPart marks it so the renderer can
    // show a friendlier 'Loading skill: <name>' chip.
    expect(parts).toEqual([
      {
        kind: "tool",
        callId: "adk-call-load-skill-1",
        name: "load_skill",
        // skill_name is the tool's real arg name, not name
        // (final-review Fix 9).
        args: { skill_name: "plotting" },
        ok: null,
      },
    ]);
  });
});

describe("mapPart — adk_request_confirmation special case", () => {
  it("unwraps a clarify-style confirmation into the clarify Extracted shape", () => {
    const parts = extractParts(confClarifyFixture.event);
    // When the originalFunctionCall is clarify, we render the same clarify
    // card — but tag the callId with the OUTER confirmation call's id, since
    // that's what the agent expects in the continuation message.
    expect(parts).toEqual([
      {
        kind: "clarify",
        callId: "adk-conf-1",
        question: "What kind of plot would you like?",
        choices: ["line", "bar", "scatter"],
      },
    ]);
  });

  it("emits a confirmation Extracted for non-clarify originalFunctionCalls", () => {
    const parts = extractParts(confYesNoFixture.event);
    expect(parts).toEqual([
      {
        kind: "confirmation",
        callId: "adk-conf-2",
        hint: "Run shell command: ls /tmp ?",
        originalCall: {
          name: "bash",
          args: { command: "ls /tmp" },
        },
        payload: null,
      },
    ]);
  });
});

describe("mapPart — adk_request_confirmation function_response", () => {
  it("maps an approve echo to a hitl_result instead of dropping it", () => {
    const part = mapPart({
      content: {
        $case: "data",
        value: {
          id: "adk-conf-2",
          name: "adk_request_confirmation",
          response: { hint: "h", confirmed: true, payload: { choice: "bar" } },
        },
      },
      metadata: { adk_type: "function_response" },
      filename: "",
      mediaType: "",
    } as Part);
    expect(part).toEqual({
      kind: "hitl_result",
      callId: "adk-conf-2",
      confirmed: true,
      text: "bar",
    });
  });

  it("maps a decline echo to a hitl_result with confirmed=false", () => {
    const part = mapPart({
      content: {
        $case: "data",
        value: {
          id: "adk-conf-3",
          name: "adk_request_confirmation",
          response: { hint: "h", confirmed: false, payload: null },
        },
      },
      metadata: { adk_type: "function_response" },
      filename: "",
      mediaType: "",
    } as Part);
    expect(part).toEqual({
      kind: "hitl_result",
      callId: "adk-conf-3",
      confirmed: false,
      text: null,
    });
  });

  it("drops a malformed confirmation response (no confirmed flag)", () => {
    const part = mapPart({
      content: {
        $case: "data",
        value: { id: "x", name: "adk_request_confirmation", response: {} },
      },
      metadata: { adk_type: "function_response" },
      filename: "",
      mediaType: "",
    } as Part);
    expect(part).toBeNull();
  });
});
