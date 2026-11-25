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

import { useEffect, useRef, useState } from "react";
import { Play } from "lucide-react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Button } from "@/components/ui/button";
import {
  buildAgentScope,
  investigationsQuery,
  startInvestigation,
} from "@/lib/aqua-api";
import {
  POLL_INTERVAL_MS,
  inFlightAge,
  listInFlightInvestigations,
  listScheduledInvestigations,
  renderOngoingNote,
  resolveNewestInvestigation,
  runButtonLabel,
  listStalledInvestigations,
} from "@/lib/investigation-state";
import { formatInstant } from "@/lib/investigation-summary";
import { textStyle } from "@/lib/typography";
import { cn } from "@/lib/utils";
import { useAgentKey } from "@/lib/agent-selection";
import { isViewerMode } from "@/lib/viewer-mode";

/**
 * Builds the inline status note shown beside the run button while a sweep is
 * polling, scheduled, or after a start attempt fails. Composed from state
 * rather than written at each turn, so a refresh landing after a failed start
 * cannot quietly wipe the reason it failed.
 *
 * Args:
 *   error: Error message from the most recent start attempt, if any.
 *   inFlight: Count of actively running or pending investigations.
 *   scheduled: Active scheduled investigations awaiting their delayed trigger.
 *
 * Returns:
 *   Note text, hover tooltip, and color class, or null when idle with no error.
 */
function buildStatusNote(
  error: string,
  inFlight: number,
  scheduled: ReturnType<typeof listScheduledInvestigations>,
): { text: string; title: string; tone: string } | null {
  if (error) {
    return {
      text: error,
      title: error,
      tone: "text-red-600 dark:text-red-400",
    };
  }
  if (inFlight > 0) {
    return {
      text: `Checking every ${Math.round(POLL_INTERVAL_MS / 1000)}s`,
      title: "Re-reading the list until the investigation finishes.",
      tone: "text-muted-foreground",
    };
  }
  const scheduledText = renderOngoingNote(scheduled, formatInstant);
  if (scheduledText) {
    return {
      text: scheduledText,
      title:
        "An investigation is already scheduled after an agent update. " +
        "Starting another investigation immediately is still allowed.",
      tone: "text-muted-foreground",
    };
  }
  return null;
}

/**
 * Builds the hover tooltip explaining why starting another investigation is
 * allowed when an unsettled record has gone stale. Kept on the button's tooltip
 * rather than inline in the toolbar because the table row itself already marks
 * stalled sweeps.
 *
 * Args:
 *   stalled: Investigations marked running whose heartbeat went stale.
 *
 * Returns:
 *   Tooltip text when at least one investigation is stalled, or undefined.
 */
function buildStalledTitle(
  stalled: ReturnType<typeof listStalledInvestigations>,
): string | undefined {
  if (stalled.length === 0) return undefined;
  const age = inFlightAge(resolveNewestInvestigation(stalled));
  const lead =
    stalled.length === 1
      ? `Last investigation stuck for ${age}`
      : `${stalled.length} investigations stuck`;
  return (
    `${lead} — nothing has updated ${stalled.length === 1 ? "it" : "them"} in ${age}, so ` +
    "the wait was given up on. Starting another investigation is allowed."
  );
}

/**
 * Run investigation: the only control that starts a sweep by hand.
 *
 * **The disabled state is derived from the investigations list, not from the
 * click.** An investigation costs money, and the list is the only thing that
 * knows about sweeps this tab did not start -- an ambient one, the cron,
 * another browser. Deriving it from server state is also what makes the guard
 * survive a reload, which a client-side "I clicked it" flag would not.
 *
 * While something is in flight the list is re-read every `POLL_INTERVAL_MS` and
 * the button's fill sweeps over the same interval, so a disabled control shows
 * a wait that is visibly finite rather than looking broken.
 *
 * Renders nothing in a read-only viewer, which starts no investigation.
 */
export function RunInvestigationButton({ className }: { className?: string }) {
  const queryClient = useQueryClient();
  const agentKey = useAgentKey();
  const [error, setError] = useState("");
  const investigations = useQuery(investigationsQuery(agentKey));

  const live = listInFlightInvestigations(investigations.data);
  const inFlight = live.length;
  const scheduled = listScheduledInvestigations(investigations.data);
  const stalled = listStalledInvestigations(investigations.data);

  const start = useMutation({
    mutationFn: () => startInvestigation(agentKey),
    onMutate: () => setError(""), // this click supersedes whatever the last said
    onSuccess: (body) => {
      if (body.error) setError(`Couldn't start it: ${body.error}`);
    },
    onError: () => setError("Couldn't start it — the agent didn't answer."),
    // Clean or refused, the list is what says whether a sweep is really going.
    onSettled: () =>
      queryClient.invalidateQueries({
        queryKey: investigationsQuery(agentKey).queryKey,
      }),
  });

  // Poll while an investigation is running or awaiting its delayed trigger.
  // `refetchInterval` is set on the shared investigations key, so the table on
  // the same page refreshes with it rather than keeping a second timer.
  useQuery({
    ...investigationsQuery(agentKey),
    refetchInterval:
      inFlight > 0 || scheduled.length > 0 ? POLL_INTERVAL_MS : false,
  });

  // A sweep just finished. Its counters, its day column and any insights it
  // clustered all moved, and none of them are read by the poll -- which fetches
  // the list alone, because four more round trips every thirty seconds to watch
  // figures that cannot change until the sweep ends is a poor trade.
  const watching = useRef(false);
  useEffect(() => {
    if (inFlight > 0) {
      watching.current = true;
      // Something is demonstrably in flight, so whatever the last start said
      // about not getting through is moot.
      setError("");
      return;
    }
    if (!watching.current) return;
    watching.current = false;
    for (const key of ["stats", "daily", "insights", "health"]) {
      void queryClient.invalidateQueries({
        queryKey: [...buildAgentScope(agentKey), key],
      });
    }
  }, [inFlight, queryClient, agentKey]);

  const starting = start.isPending;
  const label =
    inFlight > 0
      ? runButtonLabel(inFlight)
      : starting
        ? "Starting…"
        : runButtonLabel(0);
  const shown = buildStatusNote(error, inFlight, scheduled);
  const stalledTitle =
    inFlight === 0 && !error ? buildStalledTitle(stalled) : undefined;

  if (isViewerMode()) return null;

  return (
    <div className={cn("flex min-w-0 flex-wrap items-center gap-2", className)}>
      {shown && (
        <p
          title={shown.title}
          className={cn(
            textStyle.meta,
            "ml-1 max-w-[260px] truncate",
            shown.tone,
          )}
        >
          {shown.text}
        </p>
      )}
      <Button
        type="button"
        size="sm"
        variant="outline"
        onClick={() => start.mutate()}
        disabled={starting || inFlight > 0}
        aria-label="Run investigation"
        title={stalledTitle}
        className={cn(
          "relative shrink-0 gap-1.5 overflow-hidden border-primary/25 bg-primary/5 text-primary hover:bg-primary/10 hover:text-primary focus-visible:ring-inset focus-visible:ring-offset-0 [&_svg]:size-3",
          // Waiting on a sweep is a busy state, not an off one: the button keeps
          // its colour so the fill crossing it is legible, and only the pointer
          // says it cannot be pressed.
          inFlight > 0 &&
            "cursor-progress hover:bg-primary/5 disabled:pointer-events-auto disabled:opacity-100",
        )}
      >
        {inFlight > 0 && (
          // Keyed on the fetch count so each re-read restarts the animation
          // from zero: the bar reaching the far edge and the list being read are
          // one event. The duration comes from the constant the poll uses, so
          // the two cannot drift.
          <span
            key={investigations.dataUpdatedAt}
            aria-hidden
            className="pointer-events-none absolute inset-y-0 left-0 w-0 animate-[run-dwell_linear_forwards] bg-primary/15 motion-reduce:animate-none"
            style={{ animationDuration: `${POLL_INTERVAL_MS}ms` }}
          />
        )}
        <span className="relative inline-flex items-center gap-1.5">
          {inFlight === 0 && !starting && (
            <Play className="fill-current" aria-hidden="true" />
          )}
          {label}
        </span>
      </Button>
    </div>
  );
}
