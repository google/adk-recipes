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

import type { EventItem } from "./cards";
import { MINUTE, timeAgo } from "./format";

const DAY = 24 * 60 * MINUTE;
const SOON = 3 * 60 * MINUTE;

const clock = (at: number) =>
  new Date(at).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
const shortDate = (at: number) =>
  new Date(at).toLocaleDateString([], { month: "short", day: "numeric" });
const midnight = (at: number) => {
  const d = new Date(at);
  return new Date(d.getFullYear(), d.getMonth(), d.getDate()).getTime();
};

export function minutesLabel(minutes: number): string {
  const m = Math.max(1, Math.round(minutes));
  if (m < 60) return `${m} min`;
  return m % 60 ? `${Math.floor(m / 60)} hr ${m % 60} min` : `${m / 60} hr`;
}

/** `YYYY-MM-DD` all-day dates are parsed as local midnight (`Date.parse` would read them as UTC). */
export function eventTime(value: string | undefined): number {
  if (!value) return NaN;
  const date = /^(\d{4})-(\d{2})-(\d{2})$/.exec(value);
  if (date) return new Date(+date[1], +date[2] - 1, +date[3]).getTime();
  return Date.parse(value);
}

export type EventPhase = "now" | "next" | "later" | "past" | "allday";

export interface TimedEvent {
  item: EventItem;
  start: number;
  end: number;
  phase: EventPhase;
}

/** Only the first upcoming timed event is marked `next`. */
export function timedEvents(items: EventItem[], now: number): TimedEvent[] {
  let sawNext = false;
  return items.map((item) => {
    const start = eventTime(item.start);
    const end = eventTime(item.end) || start;
    let phase: EventPhase = "later";
    if (item.all_day) phase = "allday";
    else if (Number.isNaN(start)) phase = "later";
    else if (start <= now && now < end) phase = "now";
    else if (end <= now) phase = "past";
    else if (!sawNext) {
      phase = "next";
      sawNext = true;
    }
    return { item, start, end, phase };
  });
}

export function eventTimes({ item, start, end }: TimedEvent): string[] {
  if (item.all_day) return ["All day"];
  if (Number.isNaN(start)) return [];
  return end > start ? [clock(start), clock(end)] : [clock(start)];
}

export function phaseLabel(
  { phase, start, end }: TimedEvent,
  now: number,
): string | null {
  if (phase === "now" && end > now)
    return `Now · ${minutesLabel((end - now) / MINUTE)} left`;
  if (phase === "next" && start - now < SOON)
    return `In ${minutesLabel((start - now) / MINUTE)}`;
  return null;
}

export function eventDayLabel(start: number, now: number): string {
  if (Number.isNaN(start)) return "";
  const days = Math.round((midnight(start) - midnight(now)) / DAY);
  if (days === 0) return "Today";
  if (days === 1) return "Tomorrow";
  if (days === -1) return "Yesterday";
  if (days > 1 && days < 7)
    return new Date(start).toLocaleDateString([], { weekday: "long" });
  return shortDate(start);
}

export function gmailThreadUrl(id: string): string {
  return `https://mail.google.com/mail/u/0/#all/${encodeURIComponent(id)}`;
}

/** Allow only `https:` external links on cards; drop `javascript:`, `data:`, `http:`, or malformed URLs. */
export function safeHref(url: string | undefined | null): string | undefined {
  if (typeof url !== "string") return undefined;
  const trimmed = url.trim();
  if (!trimmed) return undefined;
  try {
    const parsed = new URL(trimmed);
    return parsed.protocol === "https:" ? trimmed : undefined;
  } catch {
    return undefined;
  }
}

export function senderName(from: string | undefined): string {
  const raw = (from ?? "").trim();
  const name = raw
    .replace(/<[^>]*>/, "")
    .trim()
    .replace(/^"(.*)"$/, "$1")
    .trim();
  if (name) return name;
  return raw.replace(/^<|>$/g, "");
}

export type FileKind = "doc" | "sheet" | "slides" | "pdf" | "folder" | "file";

export const FILE_KIND_LABELS: Record<FileKind, string> = {
  doc: "Document",
  sheet: "Spreadsheet",
  slides: "Presentation",
  pdf: "PDF",
  folder: "Folder",
  file: "File",
};

const MIME_KINDS: Array<[pattern: RegExp, kind: FileKind]> = [
  [/google-apps\.folder$/, "folder"],
  [/google-apps\.document$|wordprocessingml|msword|^text\/plain$/, "doc"],
  [/google-apps\.spreadsheet$|spreadsheetml|ms-excel|^text\/csv$/, "sheet"],
  [/google-apps\.presentation$|presentationml|ms-powerpoint/, "slides"],
  [/^application\/pdf$/, "pdf"],
];

export function fileKind(mimeType: string | undefined): FileKind {
  const mime = (mimeType ?? "").toLowerCase();
  return MIME_KINDS.find(([pattern]) => pattern.test(mime))?.[1] ?? "file";
}

export function editedLabel(iso: string | undefined, now = Date.now()): string {
  const when = timeAgo(iso, now);
  if (!when) return "";
  if (when === "now") return "Edited just now";
  if (when === "Yesterday") return "Edited yesterday";
  return /^\d/.test(when) ? `Edited ${when} ago` : `Edited ${when}`;
}

export function asOfLabel(iso: string | undefined, now = Date.now()): string {
  const at = iso ? Date.parse(iso) : NaN;
  if (Number.isNaN(at)) return "";
  return midnight(at) === midnight(now)
    ? `as of ${clock(at)}`
    : `as of ${shortDate(at)}`;
}
