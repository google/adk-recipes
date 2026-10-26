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
import { AlertCircle, Check, Info } from "lucide-react";
import {
  healthQuery,
  saveSchedule,
  scheduleQuery,
  type ScheduleChange,
} from "@/lib/aqua-api";
import { formatCron } from "@/lib/cron";
import { formatInstant } from "@/lib/run-summary";
import { mono, textStyle } from "@/lib/typography";
import { cn } from "@/lib/utils";
import { Button } from "@/components/ui/button";
import { FailureNote } from "@/components/ui/failure-note";
import { HelpPopover } from "@/components/ui/help-popover";
import { Input } from "@/components/ui/input";
import { Loading } from "@/components/ui/loading";
import { useAgentKey } from "@/lib/agent-selection";

const TITLE = "Investigation schedule";

/** When the selected agent is investigated: its cron, and whether its
 *  scheduled runs are on. Both are stored in its attachment, and the engine
 *  reads them on its next tick. */
export function ScheduleCard() {
  const selectedKey = useAgentKey();
  const { data, isLoading, error } = useQuery(scheduleQuery(selectedKey));
  const queryClient = useQueryClient();
  const [cronDraft, setCronDraft] = useState<string | null>(null);
  const [enabledDraft, setEnabledDraft] = useState<boolean | null>(null);
  const [savedNotice, setSavedNotice] = useState(false);

  const mutation = useMutation({
    mutationFn: (change: ScheduleChange) => saveSchedule(change),
    onSuccess: async () => {
      // The homepage's Ambient cell shows the schedule the health check reads.
      void queryClient.invalidateQueries({
        queryKey: healthQuery(selectedKey).queryKey,
      });
      // The drafts stay until the saved values are read back, so the input
      // does not show the old cron beside "Saved" in between.
      await queryClient.invalidateQueries({
        queryKey: scheduleQuery(selectedKey).queryKey,
      });
      setCronDraft(null);
      setEnabledDraft(null);
      setSavedNotice(true);
      setTimeout(() => setSavedNotice(false), 3000);
    },
  });

  if (isLoading) {
    return (
      <section className="rounded-lg border bg-card p-4">
        <Loading label="Loading the schedule…" />
      </section>
    );
  }

  if (error) {
    return (
      <section className="rounded-lg border bg-card p-4">
        <FailureNote lead="Could not load the schedule." error={error} />
      </section>
    );
  }

  if (!data?.available || !data.agent_key) {
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
          No schedule to edit: {data?.reason ?? "the agent sent no schedule."}
        </p>
      </section>
    );
  }

  const agentKey = data.agent_key;
  const currentCron = data.investigation_schedule ?? "";
  const currentEnabled = data.scheduled_trigger_enabled ?? true;
  const cron = cronDraft ?? currentCron;
  const enabled = enabledDraft ?? currentEnabled;
  const cronChanged = cron.trim() !== currentCron;
  const enabledChanged = enabled !== currentEnabled;
  const dirty = cronChanged || enabledChanged;
  const phrased = formatCron(cron);

  const discard = () => {
    setCronDraft(null);
    setEnabledDraft(null);
    mutation.reset();
  };

  const save = () =>
    mutation.mutate({
      agentKey,
      // Only what changed, so a save cannot overwrite the other field with
      // what this page read before someone else changed it.
      investigationSchedule: cronChanged ? cron.trim() : undefined,
      scheduledTriggerEnabled: enabledChanged ? enabled : undefined,
    });

  return (
    <section className="flex flex-col gap-3 rounded-lg border bg-card p-4">
      <div className="flex flex-col gap-1">
        <div className="flex items-center gap-1">
          <h2 className={textStyle.sectionTitle}>{TITLE}</h2>
          <HelpPopover topic="the investigation schedule">
            AQuA checks the schedule every 15 minutes and starts one
            investigation when the cron fell due since the previous check. A
            cron that fires more often still starts at most one investigation
            per check. Changes take effect on the next check.
          </HelpPopover>
        </div>
        <p className={textStyle.meta}>
          When AQuA investigates <span className={mono}>{data.agent_name}</span>
          , as a five-field cron in UTC.
        </p>
        {data.updated_at && (
          <p className={textStyle.meta}>
            Saved {formatInstant(data.updated_at)}
          </p>
        )}
      </div>

      <form
        className="flex flex-col gap-3"
        onSubmit={(e) => {
          e.preventDefault();
          if (dirty && cron.trim() && !mutation.isPending) save();
        }}
      >
        <div className="flex flex-wrap items-center gap-3">
          <Input
            aria-label="Cron schedule (UTC)"
            value={cron}
            onChange={(e) => setCronDraft(e.target.value)}
            placeholder="0 12 * * *"
            spellCheck={false}
            autoComplete="off"
            className={cn(mono, "w-48")}
          />
          {phrased !== cron && (
            <span className={textStyle.meta}>{phrased}</span>
          )}
        </div>

        <label className={cn(textStyle.body, "flex items-center gap-2")}>
          <input
            type="checkbox"
            checked={enabled}
            onChange={(e) => setEnabledDraft(e.target.checked)}
            className="h-3.5 w-3.5 rounded border-border text-primary focus:ring-1 focus:ring-primary"
          />
          Investigate on this schedule
        </label>

        {!enabled && (
          <p
            className={cn(
              textStyle.meta,
              "flex items-center gap-1.5 rounded-md border bg-muted/40 p-2",
            )}
          >
            <Info className="h-3.5 w-3.5 shrink-0" aria-hidden="true" />
            Scheduled investigations are off. Run investigation still starts
            one, and so does a new revision of the agent when it has an update
            trigger.
          </p>
        )}

        <div className="flex flex-wrap items-center justify-end gap-2">
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
              type="button"
              size="sm"
              variant="ghost"
              disabled={mutation.isPending}
              onClick={discard}
            >
              Discard changes
            </Button>
          )}
          <Button
            type="submit"
            size="sm"
            disabled={!dirty || !cron.trim() || mutation.isPending}
          >
            {mutation.isPending ? "Saving…" : "Save schedule"}
          </Button>
        </div>
      </form>
    </section>
  );
}
