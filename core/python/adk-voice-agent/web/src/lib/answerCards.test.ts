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
  asOfLabel,
  editedLabel,
  eventDayLabel,
  eventTime,
  eventTimes,
  fileKind,
  gmailThreadUrl,
  minutesLabel,
  phaseLabel,
  safeHref,
  senderName,
  timedEvents,
} from "./answerCards";
import type { EventItem } from "./cards";

// Local times, so the tests hold in any timezone.
const now = new Date(2026, 9, 26, 14, 0).getTime(); // Monday, Oct 26, 2:00 PM
const at = (hour: number, minute = 0, day = 26) =>
  new Date(2026, 9, day, hour, minute).toISOString();
const clock = (iso: string) =>
  new Date(iso).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });

describe("minutesLabel", () => {
  it.each([
    [43, "43 min"],
    [0.2, "1 min"],
    [60, "1 hr"],
    [90, "1 hr 30 min"],
    [120, "2 hr"],
  ])("%s -> %s", (minutes, expected) => {
    expect(minutesLabel(minutes)).toBe(expected);
  });
});

describe("eventTime", () => {
  it("reads an all-day date as local midnight", () => {
    expect(eventTime("2026-10-26")).toBe(new Date(2026, 9, 26).getTime());
  });

  it("reads a date-time, and NaN when missing", () => {
    expect(eventTime(at(9))).toBe(Date.parse(at(9)));
    expect(eventTime(undefined)).toBeNaN();
  });
});

describe("timedEvents", () => {
  const events: EventItem[] = [
    { id: "allday", start: "2026-10-26", end: "2026-10-27", all_day: true },
    { id: "done", start: at(9), end: at(10) },
    { id: "on", start: at(13, 30), end: at(14, 28) },
    { id: "soon", start: at(14, 43), end: at(15, 15) },
    { id: "after", start: at(16), end: at(17) },
  ];
  const timed = timedEvents(events, now);

  it("marks now, the first upcoming as next, and the rest", () => {
    expect(timed.map((e) => e.phase)).toEqual([
      "allday",
      "past",
      "now",
      "next",
      "later",
    ]);
  });

  it("labels the current and the next meeting", () => {
    expect(phaseLabel(timed[2], now)).toBe("Now · 28 min left");
    expect(phaseLabel(timed[3], now)).toBe("In 43 min");
    expect(phaseLabel(timed[4], now)).toBeNull();
    expect(phaseLabel(timed[0], now)).toBeNull();
  });

  it("says nothing about a next meeting hours away", () => {
    const [far] = timedEvents([{ id: "far", start: at(20), end: at(21) }], now);
    expect(far.phase).toBe("next");
    expect(phaseLabel(far, now)).toBeNull();
  });

  it("never makes an event without a start next", () => {
    expect(timedEvents([{ id: "x" }], now)[0].phase).toBe("later");
  });

  it("shows start and end, All day, or nothing", () => {
    expect(eventTimes(timed[0])).toEqual(["All day"]);
    expect(eventTimes(timed[3])).toEqual([
      clock(at(14, 43)),
      clock(at(15, 15)),
    ]);
    expect(eventTimes(timedEvents([{ id: "x" }], now)[0])).toEqual([]);
  });
});

describe("eventDayLabel", () => {
  const day = (d: number) => new Date(2026, 9, d, 9).getTime();

  it("names nearby days", () => {
    expect(eventDayLabel(day(26), now)).toBe("Today");
    expect(eventDayLabel(day(27), now)).toBe("Tomorrow");
    expect(eventDayLabel(day(25), now)).toBe("Yesterday");
    expect(eventDayLabel(day(29), now)).toBe(
      new Date(day(29)).toLocaleDateString([], { weekday: "long" }),
    );
  });

  it("gives the date a week out", () => {
    expect(eventDayLabel(day(31) + 7 * 86_400_000, now)).toBe(
      new Date(2026, 10, 7).toLocaleDateString([], {
        month: "short",
        day: "numeric",
      }),
    );
  });
});

describe("gmailThreadUrl", () => {
  it("opens the thread in Gmail", () => {
    expect(gmailThreadUrl("18f2a")).toBe(
      "https://mail.google.com/mail/u/0/#all/18f2a",
    );
  });
});

describe("senderName", () => {
  it.each([
    ['"Ana Ruiz" <ana@example.com>', "Ana Ruiz"],
    ["Ana Ruiz <ana@example.com>", "Ana Ruiz"],
    ["ana@example.com", "ana@example.com"],
    ["<ana@example.com>", "ana@example.com"],
    ["", ""],
    [undefined, ""],
  ])("%s -> %s", (from, expected) => {
    expect(senderName(from)).toBe(expected);
  });
});

describe("fileKind", () => {
  it.each([
    ["application/vnd.google-apps.document", "doc"],
    ["application/vnd.google-apps.spreadsheet", "sheet"],
    ["application/vnd.google-apps.presentation", "slides"],
    ["application/vnd.google-apps.folder", "folder"],
    ["application/pdf", "pdf"],
    [
      "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
      "doc",
    ],
    ["text/csv", "sheet"],
    ["image/png", "file"],
    [undefined, "file"],
  ])("%s -> %s", (mime, expected) => {
    expect(fileKind(mime)).toBe(expected);
  });
});

describe("editedLabel", () => {
  it.each([
    [new Date(now - 30_000).toISOString(), "Edited just now"],
    [at(11), "Edited 3h ago"],
    [at(12, 0, 25), "Edited yesterday"],
    [undefined, ""],
  ])("%s -> %s", (iso, expected) => {
    expect(editedLabel(iso, now)).toBe(expected);
  });
});

describe("asOfLabel", () => {
  it("gives the time today, else the date", () => {
    expect(asOfLabel(at(13, 14), now)).toBe(`as of ${clock(at(13, 14))}`);
    expect(asOfLabel(at(9, 0, 24), now)).toBe(
      `as of ${new Date(2026, 9, 24).toLocaleDateString([], {
        month: "short",
        day: "numeric",
      })}`,
    );
  });

  it("is empty when the time is missing or bad", () => {
    expect(asOfLabel(undefined, now)).toBe("");
    expect(asOfLabel("not a time", now)).toBe("");
  });
});

describe("safeHref", () => {
  it("allows https URLs and rejects javascript:, data:, http:, and invalid URLs", () => {
    expect(safeHref("https://calendar.google.com/event?eid=1")).toBe(
      "https://calendar.google.com/event?eid=1",
    );
    expect(safeHref("  https://docs.google.com/document/d/1  ")).toBe(
      "https://docs.google.com/document/d/1",
    );
    expect(safeHref("javascript:alert(1)")).toBeUndefined();
    expect(safeHref("JAVASCRIPT:alert(1)")).toBeUndefined();
    expect(
      safeHref("data:text/html,<script>alert(1)</script>"),
    ).toBeUndefined();
    expect(safeHref("http://example.com")).toBeUndefined();
    expect(safeHref("/relative/path")).toBeUndefined();
    expect(safeHref("")).toBeUndefined();
    expect(safeHref(undefined)).toBeUndefined();
    expect(safeHref(null)).toBeUndefined();
  });
});
