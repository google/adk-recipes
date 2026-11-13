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

import type { FocusPayload } from "./api";
import type { ThreadSummary } from "./types";

export type View =
  | { view: "home" }
  | { view: "project"; project: string }
  | { view: "thread"; threadId: string; project?: string; title?: string };

export const HOME: View = { view: "home" };

export const NAVIGATE_TOOL = "navigate";

export function isNavigationTool(name: string): boolean {
  return name === NAVIGATE_TOOL;
}

export const APP_CONNECTED_TAG = "[APP CONNECTED]";

export const appConnectedTurn = (displayName: string) =>
  `${APP_CONNECTED_TAG} ${displayName}`;

/** Preserves referential identity when the destination matches `view` so React skips re-rendering. */
export function navigate(view: View, next: View): View {
  if (next.view === "home") return view.view === "home" ? view : HOME;
  if (
    view.view === "project" &&
    next.view === "project" &&
    view.project === next.project
  ) {
    return view;
  }
  if (
    view.view === "thread" &&
    next.view === "thread" &&
    view.threadId === next.threadId &&
    view.project === next.project &&
    view.title === next.title
  ) {
    return view;
  }
  return next;
}

export function focusPayload(view: View): FocusPayload {
  switch (view.view) {
    case "home":
      return { view: "home" };
    case "project":
      return { view: "project", project: view.project };
    case "thread":
      return {
        view: "thread",
        thread_id: view.threadId,
        ...(view.project ? { project: view.project } : {}),
      };
  }
}

const text = (value: unknown) => (typeof value === "string" ? value : "");

export function navigateView(
  args: Record<string, unknown>,
  threads: ThreadSummary[],
): View | null {
  const project = text(args.project);
  const threadId = text(args.thread_id);
  switch (args.target) {
    case "home":
      return HOME;
    case "project":
      return project ? { view: "project", project } : null;
    case "thread": {
      if (!threadId) return null;
      const matched = threads.find((t) => t.thread_id === threadId);
      const owner = project || matched?.project || "";
      if (!owner) return null;
      const title = text(args.title) || matched?.title;
      return {
        view: "thread",
        threadId,
        project: owner,
        ...(title ? { title } : {}),
      };
    }
    default:
      return null;
  }
}

export function navigateApp(args: Record<string, unknown>): string | null {
  if (args.target !== "app") return null;
  return text(args.app) || null;
}
