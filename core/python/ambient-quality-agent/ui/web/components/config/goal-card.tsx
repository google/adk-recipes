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

import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { AlertCircle, Check, ChevronRight, Info } from "lucide-react";
import {
  goalQuery,
  goalVersionsQuery,
  saveGoal,
  type GoalVersionRow,
} from "@/lib/aqua-api";
import { formatInstant } from "@/lib/run-summary";
import { mono, textStyle } from "@/lib/typography";
import { cn } from "@/lib/utils";
import { Button } from "@/components/ui/button";
import { FailureNote } from "@/components/ui/failure-note";
import { GcsLink } from "@/components/ui/gcs-link";
import { HelpPopover } from "@/components/ui/help-popover";
import { Loading } from "@/components/ui/loading";
import { Textarea } from "@/components/ui/textarea";

/** Shown in the empty box: a whole goal in the shape the review uses. */
const EXAMPLE_GOAL = [
  "Our agent answers billing questions for small-business customers.",
  "What matters most: every amount, date and plan name it states must come from a tool result, and refund requests over $500 go to a human.",
  "Look hardest at messages that ask more than one thing, where the agent answers one and ignores the rest.",
  "Tone, greetings and small talk do not matter.",
].join("\n");

/** UTF-8 bytes a goal may hold; the agent refuses a longer one on save. */
const GOAL_MAX_BYTES = 8 * 1024;

const TITLE = "Developer goal";

/** The developer's goal (`goal.md`): an editor for the saved goal, and the
 * earlier versions to bring back. Every save is kept as a version. */
export function GoalCard() {
  const { data, isLoading, error } = useQuery(goalQuery());
  const { data: versionsData } = useQuery(goalVersionsQuery());
  const queryClient = useQueryClient();
  const [draft, setDraft] = useState<string | null>(null);
  const [restoredFrom, setRestoredFrom] = useState<GoalVersionRow | null>(null);
  const [savedNotice, setSavedNotice] = useState(false);

  const mutation = useMutation({
    mutationFn: (text: string) => saveGoal(text),
    onSuccess: () => {
      setDraft(null);
      setRestoredFrom(null);
      setSavedNotice(true);
      setTimeout(() => setSavedNotice(false), 3000);
      void queryClient.invalidateQueries({ queryKey: ["aqua", "goal"] });
    },
  });

  if (isLoading) {
    return (
      <section className="rounded-lg border bg-card p-4">
        <Loading label="Loading the goal…" />
      </section>
    );
  }

  if (error) {
    return (
      <section className="rounded-lg border bg-card p-4">
        <FailureNote lead="Could not load the goal." error={error} />
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
          No goal is possible right now: {data.reason}
        </p>
      </section>
    );
  }

  const current = data?.goal ?? "";
  const value = draft ?? current;
  const dirty = value.trim() !== current.trim();
  // Saving an emptied box removes the goal; the removed text stays a version.
  const removing = dirty && !value.trim();
  const bytes = new TextEncoder().encode(value.trim()).length;
  const tooLong = bytes > GOAL_MAX_BYTES;
  const versions = versionsData?.available ? (versionsData.versions ?? []) : [];
  const active = versions.find((v) => v.active);
  const earlier = versions.filter((v) => !v.active);

  const discard = () => {
    setDraft(null);
    setRestoredFrom(null);
  };

  return (
    <section className="flex flex-col gap-3 rounded-lg border bg-card p-4">
      <div className="flex items-start justify-between gap-3">
        <div className="flex flex-col gap-1">
          <div className="flex items-center gap-1">
            <h2 className={textStyle.sectionTitle}>{TITLE}</h2>
            <HelpPopover topic="the developer goal">
              Each investigation looks hardest at what the goal names. A goal
              never creates a finding on its own: findings are still judged
              against your agent&apos;s instructions and tools. Every save is
              kept as a version you can bring back.
            </HelpPopover>
          </div>
          <p className={textStyle.meta}>
            What AQuA should look hardest at when it reviews your agent&apos;s
            conversations.
          </p>
          {current && data?.version && (
            <p className={textStyle.meta}>
              {active?.last_activated_at
                ? `Saved ${formatInstant(active.last_activated_at)} · `
                : ""}
              <span className={mono}>version {data.version}</span>
            </p>
          )}
        </div>
        <GcsLink uri={data?.uri} />
      </div>

      {!current && !dirty && (
        <p
          className={cn(
            textStyle.meta,
            "flex items-center gap-1.5 rounded-md border bg-muted/40 p-2",
          )}
        >
          <Info className="h-3.5 w-3.5 shrink-0" aria-hidden="true" />
          No goal saved. Until you save one, AQuA uses only its own
          instructions.
        </p>
      )}

      {restoredFrom && dirty && (
        <p
          className={cn(
            textStyle.meta,
            "flex items-center gap-1.5 rounded-md border bg-muted/40 p-2",
          )}
        >
          <Info className="h-3.5 w-3.5 shrink-0" aria-hidden="true" />
          Loaded the version saved{" "}
          {formatInstant(
            restoredFrom.last_activated_at || restoredFrom.created_at,
          )}
          . Save to make it the goal again.
        </p>
      )}

      <Textarea
        aria-label={TITLE}
        value={value}
        onChange={(e) => setDraft(e.target.value)}
        rows={5}
        className={cn(textStyle.body, "placeholder:text-muted-foreground/50")}
        placeholder={EXAMPLE_GOAL}
      />

      <div className="flex flex-wrap items-center justify-between gap-2">
        <span className={cn(textStyle.meta, tooLong && "text-destructive")}>
          {bytes.toLocaleString()} / {GOAL_MAX_BYTES.toLocaleString()} bytes
          {tooLong && " — too long to save"}
        </span>
        <div className="flex items-center gap-2">
          {savedNotice && (
            <span
              className={cn(
                textStyle.meta,
                "flex items-center gap-1 text-emerald-500",
              )}
            >
              <Check className="h-3.5 w-3.5" /> Saved
            </span>
          )}
          {mutation.isError && (
            <span className={cn(textStyle.meta, "text-destructive")}>
              {(mutation.error as Error).message}
            </span>
          )}
          {dirty && (
            <Button
              size="sm"
              variant="ghost"
              disabled={mutation.isPending}
              onClick={discard}
            >
              Discard changes
            </Button>
          )}
          <Button
            size="sm"
            disabled={!dirty || tooLong || mutation.isPending}
            onClick={() => mutation.mutate(value)}
          >
            {mutation.isPending
              ? "Saving…"
              : removing
                ? "Remove goal"
                : "Save goal"}
          </Button>
        </div>
      </div>

      {earlier.length > 0 && (
        <PreviousVersions
          versions={earlier}
          restoreDisabled={dirty || mutation.isPending}
          onRestore={(version) => {
            setDraft(version.text);
            setRestoredFrom(version);
          }}
        />
      )}
    </section>
  );
}

/** The versions before the saved one, most recently used first. Restore puts a
 *  version's text in the editor; saving it makes that version the goal again. */
function PreviousVersions({
  versions,
  restoreDisabled,
  onRestore,
}: {
  versions: GoalVersionRow[];
  restoreDisabled: boolean;
  onRestore: (version: GoalVersionRow) => void;
}) {
  const [open, setOpen] = useState(false);
  const [shownInFull, setShownInFull] = useState<ReadonlySet<string>>(
    new Set(),
  );
  const toggleFull = (version: string) =>
    setShownInFull((prev) => {
      const next = new Set(prev);
      if (!next.delete(version)) next.add(version);
      return next;
    });

  return (
    <div className="flex flex-col border-t pt-2">
      <button
        type="button"
        className={cn(
          textStyle.label,
          "-mx-2 flex items-center gap-1.5 rounded-md px-2 py-1.5 text-left hover:bg-accent hover:text-foreground",
        )}
        aria-expanded={open}
        onClick={() => setOpen(!open)}
      >
        <ChevronRight
          className={cn(
            "h-3.5 w-3.5 transition-transform",
            open && "rotate-90",
          )}
          aria-hidden="true"
        />
        Previous versions ({versions.length})
      </button>
      {open && (
        <ol className="mt-1 flex flex-col divide-y divide-border/60">
          {versions.map((v) => {
            const long = isLongGoal(v.text);
            const full = shownInFull.has(v.version);
            return (
              <li key={v.version} className="flex items-start gap-3 py-3">
                <div className="flex min-w-0 flex-1 flex-col gap-1">
                  <p className={textStyle.meta}>
                    Saved {formatInstant(v.last_activated_at || v.created_at)}
                    <span className={mono}> · version {v.version}</span>
                  </p>
                  <p
                    className={cn(
                      textStyle.body,
                      "whitespace-pre-wrap break-words",
                      long && !full && "line-clamp-2",
                    )}
                  >
                    {v.text}
                  </p>
                  {long && (
                    <Button
                      size="sm"
                      variant="link"
                      className="h-auto self-start p-0"
                      aria-expanded={full}
                      onClick={() => toggleFull(v.version)}
                    >
                      {full ? "Show less" : "Show all"}
                    </Button>
                  )}
                </div>
                <Button
                  size="sm"
                  variant="outline"
                  className="shrink-0"
                  aria-label={`Restore version ${v.version}`}
                  title={
                    restoreDisabled
                      ? "Save or discard your changes first."
                      : undefined
                  }
                  disabled={restoreDisabled}
                  onClick={() => onRestore(v)}
                >
                  Restore
                </Button>
              </li>
            );
          })}
        </ol>
      )}
    </div>
  );
}

/** Whether a version's text is longer than the two lines shown before "Show all". */
function isLongGoal(text: string): boolean {
  return text.split("\n").length > 2 || text.length > 160;
}
