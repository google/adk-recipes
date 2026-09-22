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

import type { HorizonSessionSummary } from "./horizon-sessions";

export interface ScheduledGroup {
  label: string;
  sessions: HorizonSessionSummary[];
}

// Stable display order; groups with no sessions are dropped.
const ORDER: { label: string; match: HorizonSessionSummary["jobType"][] }[] = [
  { label: "Dream review", match: ["dream_review"] },
  { label: "Routines", match: ["routine"] },
];

export function groupByJobType(sessions: HorizonSessionSummary[]): ScheduledGroup[] {
  const sorted = [...sessions].sort((a, b) => b.lastUpdated - a.lastUpdated);
  const groups: ScheduledGroup[] = [];
  const taken = new Set<string>();

  for (const { label, match } of ORDER) {
    const inGroup = sorted.filter(
      (s) => s.jobType !== null && match.includes(s.jobType),
    );
    inGroup.forEach((s) => taken.add(s.id));
    if (inGroup.length) groups.push({ label, sessions: inGroup });
  }

  const other = sorted.filter((s) => !taken.has(s.id));
  if (other.length) groups.push({ label: "Other", sessions: other });
  return groups;
}
