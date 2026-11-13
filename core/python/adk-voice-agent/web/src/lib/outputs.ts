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

import type { AnswerCardData } from "./cards";
import { plural } from "./format";
import type { ThreadOutputLink } from "./types";

/** What each kind of output is called, for a chip with no title and for its tooltip. */
export const OUTPUT_LABELS: Record<ThreadOutputLink["kind"], string> = {
  pr: "Pull request",
  branch: "Branch on GitHub",
  doc: "Google Doc",
  sheet: "Google Sheet",
  slides: "Google Slides",
  file: "File in Drive",
  event: "Calendar event",
  draft: "Gmail draft",
};

/** The name a link's chip shows: its title, else what kind of thing it is. */
export function outputName(link: ThreadOutputLink): string {
  return link.title.trim() || (OUTPUT_LABELS[link.kind] ?? OUTPUT_LABELS.file);
}

const CARD_NOUNS: Record<AnswerCardData["kind"], [string, string]> = {
  events: ["event", "events"],
  threads: ["email", "emails"],
  files: ["file", "files"],
};

/** An answer card as a compact chip: "3 events", "1 email". */
export function cardChipLabel(card: AnswerCardData): string {
  const [one, many] = CARD_NOUNS[card.kind];
  return plural(card.items.length, one, many);
}
