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

import { asOfLabel } from "../../../lib/answerCards";
import type {
  AnswerCardData,
  CardKind,
  EventItem,
  FileItem,
  ThreadItem,
} from "../../../lib/cards";
import { EventsCard } from "./EventsCard";
import { FilesCard } from "./FilesCard";
import { ThreadsCard } from "./ThreadsCard";

/** What an empty card says, so "nothing on your calendar" is still an answer. */
const EMPTY: Record<CardKind, string> = {
  events: "No events",
  threads: "No matching email",
  files: "No matching files",
};

function Body({ kind, items }: Pick<AnswerCardData, "kind" | "items">) {
  if (items.length === 0) return <p className="answer-empty">{EMPTY[kind]}</p>;
  switch (kind) {
    case "events":
      return <EventsCard items={items as EventItem[]} />;
    case "threads":
      return <ThreadsCard items={items as ThreadItem[]} />;
    case "files":
      return <FilesCard items={items as FileItem[]} />;
  }
}

/**
 * One answer card (`web/src/lib/cards.ts`): a list of events, threads, or files, from
 * the voice's `look` (a context card on the stage) or a thread's `present_card` (in the
 * thread's card, with `asOf`). Self-contained, so it reads the same in either place.
 */
export function AnswerCard({ kind, title, items, asOf }: AnswerCardData) {
  const asOfText = asOfLabel(asOf);
  return (
    <section className="answer-card" data-kind={kind}>
      {title && <p className="answer-card-title">{title}</p>}
      <Body kind={kind} items={items} />
      {asOfText && (
        <p className="answer-card-as-of">
          <time dateTime={asOf}>{asOfText}</time>
        </p>
      )}
    </section>
  );
}
