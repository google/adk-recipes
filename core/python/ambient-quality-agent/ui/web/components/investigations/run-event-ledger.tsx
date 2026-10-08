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

import type { InvestigationEvent } from "@/lib/aqua-api";
import { headingOf } from "@/lib/aqa-markdown";
import { Markdown } from "@/components/chat/markdown";
import { mono, textStyle } from "@/lib/typography";
import { cn } from "@/lib/utils";

export function RunEventLedger({ events }: { events?: InvestigationEvent[] }) {
  if (!events || events.length === 0) {
    return (
      <p className={cn(textStyle.meta, "p-3")}>
        No events recorded for this investigation yet.
      </p>
    );
  }

  return (
    <ol className="flex flex-col divide-y divide-border/60">
      {events.map((event, i) => {
        const parsed = headingOf(event.text);
        const heading = parsed?.heading ?? event.source ?? `Step ${i + 1}`;

        return (
          <li
            // biome-ignore lint/suspicious/noArrayIndexKey: events carry no ids and created_at can repeat; the ledger only appends, so the position is stable.
            key={`${event.created_at}-${i}`}
            className="flex flex-col gap-1.5 py-3 first:pt-0 last:pb-0"
          >
            <div className="flex flex-wrap items-center justify-between gap-2">
              <span className={textStyle.itemTitle}>{heading}</span>
              <div className={cn(textStyle.meta, "flex items-center gap-2")}>
                {event.source && (
                  <span className={cn(mono, "rounded bg-muted px-1.5 py-0.5")}>
                    {event.source}
                  </span>
                )}
                <span>{new Date(event.created_at).toLocaleTimeString()}</span>
              </div>
            </div>
            <div className={cn(textStyle.meta, "text-foreground")}>
              <Markdown text={parsed?.rest || event.text} />
            </div>
          </li>
        );
      })}
    </ol>
  );
}
