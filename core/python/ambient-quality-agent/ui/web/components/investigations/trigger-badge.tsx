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

import { CalendarClock, SlidersHorizontal, UserRound } from "lucide-react";
import { resolveTriggerKind, type TriggerKind } from "@/lib/run-summary";
import { textStyle } from "@/lib/typography";
import { cn } from "@/lib/utils";

/** Each kind's word, icon, tooltip and colour. A run somebody started is
 *  violet, since it is the one they are waiting on; a sweep is muted, since
 *  the loop starts dozens of them. Violet is fixed rather than the theme's
 *  primary, which is slate by default and changes with the accent skin, and it
 *  stays apart from the red, amber and teal the run tables use for outcomes. */
const USER_TEXT = "text-violet-700 dark:text-violet-300";
const USER_TONE = "border-violet-500/40 bg-violet-500/10";

const KINDS: Record<
  TriggerKind,
  {
    label: string;
    Icon: typeof SlidersHorizontal;
    hint: string;
    text: string;
    tone: string;
  }
> = {
  custom: {
    label: "Custom",
    Icon: SlidersHorizontal,
    hint: "A custom investigation of conversations a user picked",
    text: USER_TEXT,
    tone: USER_TONE,
  },
  manual: {
    label: "Manual",
    Icon: UserRound,
    hint: "Started by a user or the CLI",
    text: USER_TEXT,
    tone: USER_TONE,
  },
  ambient: {
    label: "Scheduled",
    Icon: CalendarClock,
    hint: "Started by AQuA on its own schedule",
    text: "text-muted-foreground",
    tone: "border-border",
  },
};

/**
 * Who started a run, as a small badge: "Custom" or "Manual" for a run a user
 * asked for, "Scheduled" for one the loop started.
 */
export function TriggerBadge({
  run,
  iconOnly = false,
  className,
}: {
  run: { trigger_type?: string | null };
  /** Draw the icon alone, for a slot too narrow for the word. The word stays
   *  in the tooltip and for a screen reader. */
  iconOnly?: boolean;
  className?: string;
}) {
  const kind = KINDS[resolveTriggerKind(run)];
  const trigger = run.trigger_type?.trim();
  // The raw trigger type is kept for the tooltip: a Scheduled badge covers
  // three of them.
  const title = trigger ? `${kind.hint} (trigger: ${trigger})` : kind.hint;
  if (iconOnly) {
    return (
      <span title={title} className={cn("inline-flex shrink-0", className)}>
        <kind.Icon
          aria-hidden="true"
          className={cn("h-3.5 w-3.5", kind.text)}
        />
        <span className="sr-only">{kind.label}</span>
      </span>
    );
  }
  return (
    <span
      title={title}
      className={cn(
        textStyle.meta,
        "inline-flex shrink-0 items-center gap-1 whitespace-nowrap rounded border px-1.5",
        kind.tone,
        kind.text,
        className,
      )}
    >
      <kind.Icon aria-hidden="true" className="h-3 w-3" />
      {kind.label}
    </span>
  );
}
