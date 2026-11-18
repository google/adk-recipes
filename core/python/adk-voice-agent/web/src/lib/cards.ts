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

/** Shapes mirror `CARD_KINDS` in `app/context_cards.py` (trimmed to at most 20 items; every field except `id` is optional). */
export const CARD_KINDS = ["events", "threads", "files"] as const;
export type CardKind = (typeof CARD_KINDS)[number];

export interface EventAttendee {
  email: string;
  response?: string;
}

export interface EventItem {
  id: string;
  summary?: string;
  start?: string;
  end?: string;
  all_day?: boolean;
  location?: string;
  attendees?: EventAttendee[];
  html_link?: string;
}

export interface ThreadItem {
  id: string;
  subject?: string;
  from?: string;
  date?: string;
  snippet?: string;
  unread?: boolean;
  message_count?: number;
}

export interface FileItem {
  id: string;
  name?: string;
  mime_type?: string;
  modified_time?: string;
  web_view_link?: string;
}

export interface CardItems {
  events: EventItem;
  threads: ThreadItem;
  files: FileItem;
}

export interface AnswerCardData<K extends CardKind = CardKind> {
  kind: K;
  title?: string;
  items: CardItems[K][];
  asOf?: string;
}

export function isCardKind(value: unknown): value is CardKind {
  return (CARD_KINDS as readonly unknown[]).includes(value);
}
