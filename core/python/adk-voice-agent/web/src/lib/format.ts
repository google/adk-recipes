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

/** `141` -> `2m 21s`, `3725` -> `1h 2m`, `9` -> `9s`. */
export function formatElapsed(totalSeconds: number): string {
  const s = Math.max(0, Math.round(totalSeconds));
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  if (h > 0) return m > 0 ? `${h}h ${m}m` : `${h}h`;
  if (m > 0) return s % 60 > 0 ? `${m}m ${s % 60}s` : `${m}m`;
  return `${s}s`;
}

export function sentenceCase(text: string): string {
  return text.charAt(0).toUpperCase() + text.slice(1);
}

/** `plural(2, "file")` -> `2 files`; `plural(2, "reply", "replies")` -> `2 replies`. */
export const plural = (n: number, word: string, many = `${word}s`) =>
  `${n} ${n === 1 ? word : many}`;

export const MINUTE = 60_000;
const DAY = 24 * 60 * MINUTE;

/** `2026-10-02T18:00:00Z` -> `now`, `12m`, `3h`, `Yesterday`, `Sep 22`. */
export function timeAgo(
  iso: string | null | undefined,
  now = Date.now(),
): string {
  const at = iso ? Date.parse(iso) : NaN;
  if (Number.isNaN(at)) return "";
  const minutes = Math.floor((now - at) / MINUTE);
  if (minutes < 1) return "now";
  if (minutes < 60) return `${minutes}m`;
  if (minutes < 24 * 60) return `${Math.floor(minutes / 60)}h`;
  if (now - at < 2 * DAY) return "Yesterday";
  return new Date(at).toLocaleDateString([], {
    month: "short",
    day: "numeric",
  });
}

/** `now`, `12m`, `3h`, `2d`. */
export function relativeTime(epochSeconds: number, now: number): string {
  const age = now - epochSeconds;
  if (age < 45) return "now";
  const minutes = Math.round(age / 60);
  if (minutes < 60) return `${minutes}m`;
  const hours = Math.round(age / 3600);
  return hours < 24 ? `${hours}h` : `${Math.round(age / 86400)}d`;
}

export function timeOfDay(epochSeconds: number): string {
  return new Date(epochSeconds * 1000).toLocaleTimeString([], {
    hour: "numeric",
    minute: "2-digit",
  });
}
