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

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { AlertCircle, Trash2 } from "lucide-react";
import { deleteMemory, memoriesQuery, type MemoryRow } from "@/lib/aqua-api";
import { formatInstant } from "@/lib/run-summary";
import { Button } from "@/components/ui/button";
import { FailureNote } from "@/components/ui/failure-note";
import { GcsLink } from "@/components/ui/gcs-link";
import { HelpPopover } from "@/components/ui/help-popover";
import { Loading } from "@/components/ui/loading";
import { textStyle } from "@/lib/typography";
import { cn } from "@/lib/utils";

const TITLE = "Memory";

/** What the developer asked AQuA to remember about their agent, one per object
 *  under `memories/`. The chat stores them when asked; this card lists them
 *  and deletes them. */
export function MemoryCard() {
  const { data, isLoading, error } = useQuery(memoriesQuery());
  const queryClient = useQueryClient();
  const remove = useMutation({
    mutationFn: deleteMemory,
    onSettled: () =>
      void queryClient.invalidateQueries({ queryKey: ["aqua", "memories"] }),
  });

  if (isLoading) {
    return (
      <section className="rounded-lg border bg-card p-4">
        <Loading label="Loading the memories…" />
      </section>
    );
  }

  if (error) {
    return (
      <section className="rounded-lg border bg-card p-4">
        <FailureNote lead="Could not load the memories." error={error} />
      </section>
    );
  }

  if (data && !data.available) {
    return (
      <section className="flex flex-col gap-2 rounded-lg border border-amber-500/40 bg-card p-4">
        <h2 className={textStyle.sectionTitle}>{TITLE}</h2>
        <p
          className={cn(
            textStyle.meta,
            "flex items-center gap-1.5 text-amber-500",
          )}
        >
          <AlertCircle className="h-4 w-4 shrink-0" />
          The memories cannot be read: {data.reason}
        </p>
      </section>
    );
  }

  const entries = data?.memories ?? [];

  return (
    <section className="flex flex-col gap-3 rounded-lg border bg-card p-4">
      <div className="flex items-start justify-between gap-3">
        <div className="flex flex-col gap-1">
          <div className="flex items-center gap-1">
            <h2 className={textStyle.sectionTitle}>{TITLE}</h2>
            <HelpPopover topic="memory">
              What AQuA should know when working on your agent, such as which
              file holds its prompt or a telemetry query that worked. The chat
              stores one when you ask it to remember something, and uses it from
              then on. Delete removes it.
            </HelpPopover>
          </div>
          <p className={textStyle.meta}>
            What AQuA remembers about your agent.
          </p>
        </div>
        <GcsLink uri={data?.uri} />
      </div>

      {entries.length === 0 ? (
        <p className={textStyle.meta}>
          No memories yet. Ask the chat to remember something about your agent.
        </p>
      ) : (
        <ul className="flex flex-col divide-y divide-border/60">
          {entries.map((entry) => (
            <MemoryItem
              key={entry.id}
              entry={entry}
              disabled={remove.isPending}
              onDelete={() => remove.mutate(entry.id)}
            />
          ))}
        </ul>
      )}

      {remove.error && (
        <p className={cn(textStyle.meta, "text-destructive")}>
          {(remove.error as Error).message}
        </p>
      )}
    </section>
  );
}

function MemoryItem({
  entry,
  disabled,
  onDelete,
}: {
  entry: MemoryRow;
  disabled: boolean;
  onDelete: () => void;
}) {
  return (
    <li className="flex items-start justify-between gap-3 py-3">
      <div className="flex min-w-0 flex-1 flex-col gap-1">
        {/* Plain text on purpose: the chat worded it, so nothing in it is rendered. */}
        <p className={cn(textStyle.body, "whitespace-pre-wrap break-words")}>
          {entry.text}
        </p>
        <p className={textStyle.meta}>
          Remembered {formatInstant(entry.created_at)}
          {entry.source && ` · from ${describeSource(entry.source)}`}
        </p>
      </div>
      <Button
        size="sm"
        variant="ghost"
        className="h-8 shrink-0 px-2 text-muted-foreground hover:text-destructive"
        disabled={disabled}
        onClick={onDelete}
        aria-label={`Delete memory: ${entry.text}`}
        title="Delete"
      >
        <Trash2 className="h-3.5 w-3.5" />
      </Button>
    </li>
  );
}

/** A chat session id is a UUID; its first 8 characters are enough to tell
 *  chats apart. Any other source is shown as is. */
function describeSource(source: string): string {
  return /^[0-9a-f]{8}-[0-9a-f-]{27}$/i.test(source)
    ? `chat ${source.slice(0, 8)}`
    : source;
}
