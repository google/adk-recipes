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

import type { AppInfo } from "./types";

export const GOOGLE_APP = "google_workspace";

export interface StarterSuggestion {
  prompt: string;
  meta: string;
  app?: string;
}

const SUGGESTIONS: StarterSuggestion[] = [
  {
    prompt: "Have an agent address the review comments on my PR",
    meta: "Works in the background with GitHub",
    app: "github",
  },
  {
    prompt: "What’s on my calendar, and is anything urgent in my inbox?",
    meta: "Calendar and Gmail",
    app: GOOGLE_APP,
  },
  {
    prompt:
      "Research the best e-bikes under $2,000 and write up the trade-offs",
    meta: "Searches the web and writes it up",
  },
];

export function starterSuggestions(apps: AppInfo[]): StarterSuggestion[] {
  const connected = new Set(apps.filter((a) => a.connected).map((a) => a.name));
  return SUGGESTIONS.filter((s) => !s.app || connected.has(s.app));
}

const CONNECT_LINES: Record<string, string> = {
  [GOOGLE_APP]: "Connect Google so I can check your calendar, email and Drive",
};

export const connectLine = (app: Pick<AppInfo, "name" | "display_name">) =>
  CONNECT_LINES[app.name] ??
  `Connect ${app.display_name} so I can use it for you`;

export function appToConnect(apps: AppInfo[]): AppInfo | undefined {
  return apps.find((a) => a.name === GOOGLE_APP && !a.connected);
}
