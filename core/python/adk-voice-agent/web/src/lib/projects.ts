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

import type { CSSProperties } from "react";
import { sentenceCase } from "./format";
import type { ProjectSummary, ThreadSummary } from "./types";

export const HOME_RECENT_LIMIT = 5;

export interface HomeThreadGroups {
  needsYou: ThreadSummary[];
  running: ThreadSummary[];
  recent: ThreadSummary[];
}

export function groupHomeThreads(
  threads: ThreadSummary[],
  recentLimit = HOME_RECENT_LIMIT,
): HomeThreadGroups {
  const needsYou = threads
    .filter((t) => t.status === "awaiting_approval" || t.is_gate)
    .sort((a, b) => a.updated_at - b.updated_at);
  const running = threads
    .filter((t) => t.status === "running" || (t.is_running && !t.is_gate))
    .sort((a, b) => b.updated_at - a.updated_at);
  const recent = threads
    .filter((t) => t.status === "idle" && !t.is_stale)
    .sort((a, b) => (b.ended_at ?? b.updated_at) - (a.ended_at ?? a.updated_at))
    .slice(0, recentLimit);
  return { needsYou, running, recent };
}

/** Whether a thread awaiting approval is hidden off the current screen (on Home, false because Home shows all `needsYou`). */
export function needsYouElsewhere(
  threads: ThreadSummary[],
  scope?: { project?: string; threadId?: string },
): boolean {
  const gates = threads.filter(
    (t) => t.status === "awaiting_approval" || t.is_gate,
  );
  if (!scope) return false;
  if (scope.threadId) {
    return gates.some((t) => t.thread_id !== scope.threadId);
  }
  if (scope.project) {
    return gates.some((t) => t.project !== scope.project);
  }
  return false;
}

export const RECENT_SHOWN = 5;

export function drawerSections(projects: ProjectSummary[]): {
  needYou: ProjectSummary[];
  places: ProjectSummary[];
  recent: ProjectSummary[];
} {
  const calm = projects.filter((p) => p.needs_you === 0);
  return {
    needYou: projects.filter((p) => p.needs_you > 0),
    places: calm.filter((p) => p.kind !== "scratch"),
    recent: calm.filter((p) => p.kind === "scratch"),
  };
}

export function recentRows(
  recent: ProjectSummary[],
  expanded: boolean,
): { shown: ProjectSummary[]; hidden: number } {
  const shown = expanded ? recent : recent.slice(0, RECENT_SHOWN);
  return { shown, hidden: recent.length - shown.length };
}

export function byActivity(projects: ProjectSummary[]): ProjectSummary[] {
  return [...projects].sort(
    (a, b) => Date.parse(b.last_activity_at) - Date.parse(a.last_activity_at),
  );
}

export function searchProjects(
  projects: ProjectSummary[],
  query: string,
): ProjectSummary[] {
  const words = query.toLowerCase().split(/\s+/).filter(Boolean);
  if (words.length === 0) return projects;
  return projects.filter((p) => {
    const haystack = `${p.title} ${p.status_line} ${p.id}`.toLowerCase();
    return words.every((w) => haystack.includes(w));
  });
}

/** Fallback display title before a project summary loads (`scratch/2026-10-25-hello-note` -> `Hello note`). */
export function folderTitle(id: string): string {
  const leaf = id.split("/").filter(Boolean).pop() ?? id;
  const words = leaf
    .replace(/^\d{4}-\d{2}-\d{2}-/, "")
    .replace(/[-_]+/g, " ")
    .trim();
  return sentenceCase(words || leaf);
}

export function tintStyle(tint: number): CSSProperties {
  return { "--project-hue": String(Math.round(tint) % 360) } as CSSProperties;
}

/** Overlay `/state` threads onto a project's fetched history so new or running threads appear before the project query refetches. */
export function liveProjectThreads(
  fetched: ThreadSummary[],
  project: string,
  stateThreads: ThreadSummary[],
): ThreadSummary[] {
  const byId = new Map<string, ThreadSummary>();
  for (const t of fetched) {
    byId.set(t.thread_id, t);
  }
  for (const t of stateThreads) {
    if (t.project === project) {
      byId.set(t.thread_id, t);
    }
  }
  return [...byId.values()].sort((a, b) => b.updated_at - a.updated_at);
}

export function sameTitle(a: string, b: string): boolean {
  const norm = (t: string) =>
    t
      .toLowerCase()
      .replace(/[^\p{L}\p{N}]+/gu, " ")
      .trim();
  return norm(a) === norm(b);
}
