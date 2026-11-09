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

import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import { LiveStatus } from "../components/stage/LiveStatus";
import { ApprovalBox } from "../components/stage/thread/ThreadReply";
import { ApiError, plainErrorMessage } from "./api";
import { formatElapsed, plural, sentenceCase } from "./format";
import { isConnected, type LiveAudioState } from "./live-audio-client";
import {
  describePermission,
  formatPermissionArguments,
  RISK_LABELS,
} from "./permissions";

describe("formatElapsed", () => {
  it.each([
    [9, "9s"],
    [0, "0s"],
    [-5, "0s"],
    [60, "1m"],
    [141, "2m 21s"],
    [3600, "1h"],
    [3725, "1h 2m"],
    [59.6, "1m"],
  ])("%s seconds -> %s", (seconds, expected) => {
    expect(formatElapsed(seconds)).toBe(expected);
  });
});

describe("text helpers", () => {
  it("sentence-cases and pluralizes", () => {
    expect(sentenceCase("merge into main")).toBe("Merge into main");
    expect(sentenceCase("")).toBe("");
    expect(plural(1, "file")).toBe("1 file");
    expect(plural(0, "file")).toBe("0 files");
    expect(plural(2, "file")).toBe("2 files");
  });
});

describe("describePermission", () => {
  it("uses the known wording for mcp_gates globs", () => {
    const email = describePermission("gmail.send*");
    expect(email).toMatchObject({
      title: "Send an email",
      risk: "high",
      known: true,
    });
    expect(describePermission("google_calendar.create*")).toMatchObject({
      title: "Create a calendar event",
      risk: "high",
      known: true,
    });
    expect(describePermission("github.create_pull_request*").risk).toBe(
      "medium",
    );
    expect(describePermission("github.push*").risk).toBe("high");
    expect(describePermission("github.update_issue_body*")).toMatchObject({
      title: "Update on GitHub",
      risk: "medium",
      known: true,
    });
    expect(RISK_LABELS[email.risk]).toBe("High risk");
  });

  it("falls back to Approve this action with high risk for unknown globs", () => {
    expect(describePermission("custom.unknown*")).toEqual({
      title: "Approve this action",
      risk: "high",
      known: false,
    });
    expect(describePermission(undefined)).toEqual({
      title: "Approve this action",
      risk: "high",
      known: false,
    });
  });

  it("formats full request arguments when present", () => {
    expect(formatPermissionArguments(undefined)).toBeNull();
    expect(formatPermissionArguments({})).toBeNull();
    expect(
      formatPermissionArguments({ to: "a@example.com", subject: "Hi" }),
    ).toBe('{\n  "to": "a@example.com",\n  "subject": "Hi"\n}');
  });

  it("renders tool approvals without $ prompt and with Show full request toggle", () => {
    const html = renderToStaticMarkup(
      createElement(ApprovalBox, {
        permission: {
          action_class: "gmail.send*",
          command: "gmail.send_message to=a@example.com",
          key: "k1",
          kind: "tool",
          arguments: { to: "a@example.com", body: "hello" },
        },
        onApprove: () => {},
        onDecline: () => {},
      }),
    );
    expect(html).toContain("Send an email");
    expect(html).toContain("High risk");
    expect(html).not.toContain("thread-command-prompt");
    expect(html).toContain("Show full request");
  });
});

describe("ApiError", () => {
  it("uses server detail when present and plain copy when absent", () => {
    const withDetail = new ApiError(400, "Permission key is required.");
    expect(withDetail.status).toBe(400);
    expect(withDetail.detail).toBe("Permission key is required.");
    expect(withDetail.message).toBe("Permission key is required.");

    const conflict = new ApiError(409);
    expect(conflict.detail).toBeUndefined();
    expect(conflict.message).toBe("That request is out of date. Try again.");

    const serverErr = new ApiError(500);
    expect(serverErr.message).toBe("Something went wrong. Try again.");
    expect(serverErr.message).not.toMatch(/DELETE|POST|GET|500/);
    expect(plainErrorMessage(serverErr)).toBe(
      "Something went wrong. Try again.",
    );
  });
});

describe("LiveStatus", () => {
  const baseLive: LiveAudioState = {
    status: "idle",
    substatus: "Tap Live to connect voice",
    audioEnergyRef: { current: 0 },
    activeToolCall: null,
    startSession: async () => {},
    toggleHold: () => {},
    endSession: () => {},
    sendTextTurn: () => {},
  };

  it("renders mic_blocked and error states in LiveStatus", () => {
    expect(isConnected("mic_blocked")).toBe(true);
    expect(isConnected("error")).toBe(false);

    const micHtml = renderToStaticMarkup(
      createElement(LiveStatus, {
        live: { ...baseLive, status: "mic_blocked" },
      }),
    );
    expect(micHtml).toContain("Microphone blocked: allow it in your browser");

    const errHtml = renderToStaticMarkup(
      createElement(LiveStatus, {
        live: {
          ...baseLive,
          status: "error",
          substatus: "Couldn't connect. Tap Live to try again.",
        },
      }),
    );
    expect(errHtml).toContain("Tap Live to try again.");
  });
});
