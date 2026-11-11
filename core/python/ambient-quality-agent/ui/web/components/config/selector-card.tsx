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

import { useId, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { AlertCircle, Check } from "lucide-react";
import {
  checkSelector,
  effectiveConfigQuery,
  saveSelector,
  type EffectiveConfigResponse,
  type SelectorCheck,
} from "@/lib/aqua-api";
import { readString } from "@/lib/config-fields";
import { mono, textStyle } from "@/lib/typography";
import { cn } from "@/lib/utils";
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
  AlertDialogTrigger,
} from "@/components/ui/alert-dialog";
import { Button } from "@/components/ui/button";
import { FailureNote } from "@/components/ui/failure-note";
import { HelpPopover } from "@/components/ui/help-popover";
import { Loading } from "@/components/ui/loading";
import { Textarea } from "@/components/ui/textarea";
import { useAgentKey } from "@/lib/agent-selection";

const TITLE = "Investigation selector";

const SELECTOR_MODE = "session_review";

const STARTER_HINT =
  "This starts as the query the random sample uses. Add conditions to narrow it.";

const SAVE_HINTS = {
  unchecked: "Check the selector to save.",
  unchanged: "This selector is already saved.",
};

interface CheckedSelector {
  sql: string;
  result: SelectorCheck;
}

/** Renders the Investigation selector card for the selected agent. */
export function SelectorCard() {
  const selectedKey = useAgentKey();
  const configQuery = effectiveConfigQuery(selectedKey);
  const { data, isLoading, error } = useQuery(configQuery);
  const queryClient = useQueryClient();
  // null when not editing.
  const [draft, setDraft] = useState<string | null>(null);
  const [checked, setChecked] = useState<CheckedSelector | null>(null);
  const [confirmingClear, setConfirmingClear] = useState(false);
  const [savedNotice, setSavedNotice] = useState(false);
  const boxId = useId();
  const formId = useId();

  const config = data?.config ?? {};
  const agentKey = readString(config, "agent_key");

  const check = useMutation({
    mutationFn: (sql: string) => checkSelector(agentKey, sql),
  });

  const save = useMutation({
    mutationFn: (sql: string) => saveSelector(agentKey, sql),
    onSuccess: (reply, sql) => {
      // Another agent instance can serve the old attachment on refetch.
      queryClient.setQueryData<EffectiveConfigResponse>(
        configQuery.queryKey,
        (old) =>
          old?.config
            ? {
                ...old,
                config: { ...old.config, investigation_selector_sql: sql },
              }
            : old,
      );
      setDraft(null);
      setChecked(null);
      setConfirmingClear(false);
      if (!reply.written) return;
      setSavedNotice(true);
      setTimeout(() => setSavedNotice(false), 3000);
    },
  });

  if (isLoading) {
    return (
      <section className="rounded-lg border bg-card p-4">
        <Loading label="Loading the investigation selector…" />
      </section>
    );
  }

  if (error) {
    return (
      <section className="rounded-lg border bg-card p-4">
        <FailureNote
          lead="Could not load the investigation selector."
          error={error}
        />
      </section>
    );
  }

  // The Telemetry card already says how to attach one.
  if (!agentKey) return null;

  const agentName = readString(config, "observed_agent_name");
  const mode = readString(config, "quality_analysis_mode");
  const stored = readString(config, "investigation_selector_sql");
  const starter = data?.starter_selector ?? "";
  const selectorTable = nameSelectorTable(config);
  const editing = draft !== null;
  const result = checked && checked.sql === draft ? checked.result : undefined;
  const saveable = Boolean(
    result && !result.refusal && result.changes.length > 0,
  );
  // No hint when the selector is refused: the verdict already explains why.
  const saveHint = !result
    ? SAVE_HINTS.unchecked
    : !result.refusal && result.changes.length === 0
      ? SAVE_HINTS.unchanged
      : "";

  const cancel = () => {
    setDraft(null);
    setChecked(null);
    check.reset();
    save.reset();
  };

  const header = (
    <div className="flex items-center gap-1">
      <h2 className={textStyle.sectionTitle}>{TITLE}</h2>
      <HelpPopover topic="the investigation selector">
        A check first tests that AQuA&apos;s service account can read the
        telemetry table. Then it dry-runs the selector in BigQuery, which
        validates it without reading rows. Every investigation runs the same
        check before its queries. Nothing changes until you save.
      </HelpPopover>
    </div>
  );

  if (mode !== SELECTOR_MODE) {
    return (
      <section className="flex flex-col gap-1 rounded-lg border bg-card p-4">
        {header}
        <p className={textStyle.meta}>
          A selector needs <span className={mono}>{SELECTOR_MODE}</span> mode.
          This agent runs <span className={mono}>{mode || "—"}</span>.
        </p>
      </section>
    );
  }

  return (
    <section className="flex flex-col gap-4 rounded-lg border bg-card p-4">
      <div className="flex flex-col gap-1">
        {header}
        <p className={textStyle.meta}>
          {stored ? (
            "Investigations review only the conversations this selector picks from the telemetry table."
          ) : (
            <>
              Investigations review a random sample of{" "}
              <span className={mono}>{agentName}</span>&apos;s conversations
              from the telemetry table.
            </>
          )}
        </p>
      </div>

      {editing ? (
        <form
          id={formId}
          className="flex flex-col gap-2"
          onSubmit={(e) => {
            e.preventDefault();
            if (!draft.trim() || check.isPending) return;
            // Per call, so that a reset on Cancel drops a late answer.
            check.mutate(draft, {
              onSuccess: (result, sql) => setChecked({ sql, result }),
            });
          }}
        >
          <label htmlFor={boxId} className={textStyle.label}>
            Selector
          </label>
          <Textarea
            id={boxId}
            value={draft}
            onChange={(e) => {
              setDraft(e.target.value);
              check.reset();
              save.reset();
            }}
            rows={6}
            spellCheck={false}
            autoComplete="off"
            className={cn(textStyle.code, "min-h-32")}
          />
          {!stored && starter && (
            <p className={textStyle.meta}>{STARTER_HINT}</p>
          )}
          <p className={textStyle.meta}>
            Write one <span className={mono}>SELECT</span> that returns{" "}
            <span className={mono}>target_id</span>. Use{" "}
            <span className={mono}>@window_start</span>,{" "}
            <span className={mono}>@window_end</span> and{" "}
            <span className={mono}>@agent_name</span>. Query no table other than{" "}
            <span className={mono}>{selectorTable}</span>.
          </p>
        </form>
      ) : (
        stored && (
          <pre
            className={cn(
              textStyle.code,
              "whitespace-pre-wrap break-all rounded-md border bg-muted/40 p-2",
            )}
          >
            {stored}
          </pre>
        )
      )}

      {editing && result && <Verdict result={result} />}

      <div className="flex flex-wrap items-center justify-end gap-2">
        {editing ? (
          <>
            {saveHint && <span className={textStyle.meta}>{saveHint}</span>}
            {check.isError && (
              <span
                role="alert"
                className={cn(textStyle.meta, "text-destructive")}
              >
                {(check.error as Error).message}
              </span>
            )}
            {save.isError && (
              <span
                role="alert"
                className={cn(textStyle.meta, "text-destructive")}
              >
                {(save.error as Error).message}
              </span>
            )}
            <Button type="button" size="sm" variant="ghost" onClick={cancel}>
              Cancel
            </Button>
            <Button
              type="submit"
              form={formId}
              size="sm"
              variant={result ? "outline" : "default"}
              disabled={!draft.trim() || check.isPending}
            >
              {check.isPending ? "Checking…" : "Check selector"}
            </Button>
            <Button
              type="button"
              size="sm"
              disabled={!saveable || check.isPending || save.isPending}
              onClick={() => save.mutate(draft)}
            >
              {save.isPending ? "Saving…" : "Save selector"}
            </Button>
          </>
        ) : (
          <>
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
            {stored && (
              <AlertDialog
                open={confirmingClear}
                onOpenChange={(open) => {
                  setConfirmingClear(open);
                  if (!open) save.reset();
                }}
              >
                <AlertDialogTrigger asChild>
                  <Button type="button" size="sm" variant="outline">
                    Clear selector
                  </Button>
                </AlertDialogTrigger>
                <AlertDialogContent>
                  <AlertDialogHeader>
                    <AlertDialogTitle>Clear the selector?</AlertDialogTitle>
                    <AlertDialogDescription>
                      Investigations will review a random sample of
                      conversations again.
                    </AlertDialogDescription>
                  </AlertDialogHeader>
                  {save.isError && (
                    <p
                      role="alert"
                      className={cn(textStyle.meta, "text-destructive")}
                    >
                      {(save.error as Error).message}
                    </p>
                  )}
                  <AlertDialogFooter>
                    <AlertDialogCancel disabled={save.isPending}>
                      Keep selector
                    </AlertDialogCancel>
                    <AlertDialogAction
                      disabled={save.isPending}
                      onClick={(e) => {
                        // Stay open until the save answers.
                        e.preventDefault();
                        save.mutate("");
                      }}
                      className="bg-destructive text-destructive-foreground hover:bg-destructive/90"
                    >
                      {save.isPending ? "Clearing…" : "Clear"}
                    </AlertDialogAction>
                  </AlertDialogFooter>
                </AlertDialogContent>
              </AlertDialog>
            )}
            <Button
              type="button"
              size="sm"
              variant="outline"
              onClick={() => {
                save.reset();
                setDraft(stored || starter);
              }}
            >
              {stored ? "Change selector" : "Add selector"}
            </Button>
          </>
        )}
      </div>
    </section>
  );
}

function Verdict({ result }: { result: SelectorCheck }) {
  const refused = Boolean(result.refusal);
  const Icon = refused ? AlertCircle : Check;
  return (
    <div
      role={refused ? "alert" : "status"}
      className={cn(
        "flex items-start gap-2 rounded-md border p-3",
        refused
          ? "border-destructive/30 bg-destructive/5"
          : "border-emerald-500/30 bg-emerald-500/5",
      )}
    >
      <Icon
        className={cn(
          "mt-0.5 h-4 w-4 shrink-0",
          refused ? "text-destructive" : "text-emerald-500",
        )}
        aria-hidden="true"
      />
      <div className="flex min-w-0 flex-col gap-0.5">
        <p className={textStyle.itemTitle}>
          {refused
            ? "AQuA can't use this selector"
            : "AQuA can run this selector"}
        </p>
        <p className={cn(textStyle.meta, "[overflow-wrap:anywhere]")}>
          {refused
            ? result.refusal
            : result.changes.length
              ? "Save to use it."
              : "AQuA already uses this selector."}
        </p>
      </div>
    </div>
  );
}

/** Mirrors `selector.resolve_selector_table_id`. */
function nameSelectorTable(config: Record<string, unknown>): string {
  const table =
    readString(config, "telemetry_ingestion_source") === "cloud_logging"
      ? "completions_view"
      : readString(config, "telemetry_table");
  return `${readString(config, "telemetry_dataset")}.${table}`;
}
