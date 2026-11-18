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

import {
  Calendar,
  File,
  FileText,
  GitBranch,
  GitPullRequest,
  LayoutList,
  Mail,
  Presentation,
  Sheet,
} from "lucide-react";
import type { ThreadOutputLink } from "../../../lib/api";
import type { AnswerCardData } from "../../../lib/cards";
import { cardChipLabel, OUTPUT_LABELS, outputName } from "../../../lib/outputs";
import { AnswerCard } from "../../a2ui/answer/AnswerCard";

const OUTPUT_ICONS: Record<ThreadOutputLink["kind"], typeof File> = {
  pr: GitPullRequest,
  branch: GitBranch,
  doc: FileText,
  sheet: Sheet,
  slides: Presentation,
  file: File,
  event: Calendar,
  draft: Mail,
};

export function ThreadCards({
  cards = [],
  endedAt,
}: {
  cards?: AnswerCardData[];
  endedAt?: number | null;
}) {
  if (cards.length === 0) return null;
  const asOf = endedAt ? new Date(endedAt * 1000).toISOString() : undefined;
  return (
    <>
      {cards.map((card, idx) => (
        <AnswerCard
          key={`${card.kind}-${card.title ?? idx}`}
          {...card}
          asOf={card.asOf ?? asOf}
        />
      ))}
    </>
  );
}

/**
 * What a thread (or one turn) made, as chips that open the real thing: its PR or
 * branch, Docs, Sheets, Slides, Drive files, calendar events, and Gmail drafts.
 * `cards` adds a chip per answer card ("3 events") where the cards themselves
 * aren't shown (Home); `onOpenCards` is what tapping one does.
 */
export function ThreadOutputs({
  links,
  cards = [],
  onOpenCards,
}: {
  links: ThreadOutputLink[];
  cards?: AnswerCardData[];
  onOpenCards?: () => void;
}) {
  if (links.length === 0 && cards.length === 0) return null;
  return (
    <section className="turn-outputs" aria-label="What it made">
      {links.map((link) => {
        const Icon = OUTPUT_ICONS[link.kind] ?? File;
        const name = outputName(link);
        const label = OUTPUT_LABELS[link.kind] ?? OUTPUT_LABELS.file;
        return (
          <a
            key={link.url}
            className="turn-chip"
            data-kind={link.kind}
            href={link.url}
            target="_blank"
            rel="noreferrer"
            title={name === label ? label : `${name} · ${label}`}
          >
            <Icon size={14} aria-hidden />
            <span className="turn-chip-name">{name}</span>
          </a>
        );
      })}
      {cards.map((card, idx) => (
        <button
          key={`${card.kind}-${card.title ?? idx}`}
          type="button"
          className="turn-chip"
          data-kind="card"
          title={card.title}
          onClick={onOpenCards}
        >
          <LayoutList size={14} aria-hidden />
          <span className="turn-chip-name">{cardChipLabel(card)}</span>
        </button>
      ))}
    </section>
  );
}
