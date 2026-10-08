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
import { runsQuery, startInvestigation } from "@/lib/aqua-api";
import {
  POLL_INTERVAL_MS,
  inFlightAge,
  inFlightRuns,
  newestRun,
  runButtonLabel,
  stalledRuns,
} from "@/lib/runstate";
import { textStyle } from "@/lib/typography";
import { cn } from "@/lib/utils";

/** What the note says, and why. Composed from state rather than written at each
 *  turn, so a refresh landing after a failed start cannot quietly wipe the
 *  reason it failed -- and the three things it can say cannot fight over the
 *  one slot. */
function note(
  error: string,
  inFlight: number,
  stalled: ReturnType<typeof stalledRuns>,
): { text: string; title: string; error: boolean } | null {
  if (error) return { text: error, title: error, error: true };
  if (inFlight > 0) {
    return {
      text: `Checking every ${Math.round(POLL_INTERVAL_MS / 1000)}s`,
      title: "Re-reading the list until the investigation finishes.",
      error: false,
    };
  }
  if (stalled.length) {
    // Why the button is available with a "running" row above it.
    const age = inFlightAge(newestRun(stalled));
    return {
      text:
        stalled.length === 1
          ? `Last investigation stuck for ${age}`
          : `${stalled.length} investigations stuck`,
      title:
        `Nothing has updated ${stalled.length === 1 ? "it" : "them"} in ${age}, so ` +
        "the wait was given up on. Starting another investigation is allowed.",
      error: false,
    };
  }
  return null;
}

/**
 * Run investigation: the only control that starts a sweep by hand.
 *
 * **The disabled state is derived from the runs list, not from the click.** A
 * run costs money, and the list is the only thing that knows about sweeps this
 * tab did not start -- an ambient one, the cron, another browser. Deriving it
 * from server state is also what makes the guard survive a reload, which a
 * client-side "I clicked it" flag would not.
 *
 * While something is in flight the list is re-read every `POLL_INTERVAL_MS` and
 * the button's fill sweeps over the same interval, so a disabled control shows
 * a wait that is visibly finite rather than looking broken.
 */
export function RunInvestigationButton({ className }: { className?: string }) {
  const queryClient = useQueryClient();
  const [error, setError] = useState("");
  const runs = useQuery(runsQuery());

  const live = inFlightRuns(runs.data);
  const inFlight = live.length;
  const stalled = stalledRuns(runs.data);

  const start = useMutation({
    mutationFn: startInvestigation,
    onMutate: () => setError(""), // this click supersedes whatever the last said
    onSuccess: (body) => {
      if (body.error) setError(`Couldn't start it: ${body.error}`);
    },
    onError: () => setError("Couldn't start it — the agent didn't answer."),
    // Clean or refused, the list is what says whether a sweep is really going.
    onSettled: () =>
      queryClient.invalidateQueries({ queryKey: runsQuery().queryKey }),
  });

  // Poll only while there is something to wait for. `refetchInterval` is set on
  // the shared runs key, so the table on the same page refreshes with it rather
  // than keeping a second timer of its own.
  useQuery({
    ...runsQuery(),
    refetchInterval: inFlight > 0 ? POLL_INTERVAL_MS : false,
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
      void queryClient.invalidateQueries({ queryKey: ["aqua", key] });
    }
  }, [inFlight, queryClient]);

  const starting = start.isPending;
  const label =
    inFlight > 0
      ? runButtonLabel(inFlight)
      : starting
        ? "Starting…"
        : runButtonLabel(0);
  const shown = note(error, inFlight, stalled);

  return (
    <div className={cn("flex shrink-0 items-center gap-3", className)}>
      {shown && (
        <p
          title={shown.title}
          className={cn(
            textStyle.meta,
            "max-w-[260px] truncate",
            shown.error ? "text-red-400" : "text-muted-foreground",
          )}
        >
          {shown.text}
        </p>
      )}
      <button
        type="button"
        onClick={() => start.mutate()}
        disabled={starting || inFlight > 0}
        aria-label="Run investigation"
        className={cn(
          "relative min-w-[148px] overflow-hidden rounded-lg px-3 py-1.5",
          textStyle.body,
          "bg-primary font-medium text-primary-foreground",
          // Waiting on a sweep is a busy state, not an off one: the button keeps
          // its colour so the fill crossing it is legible, and only the pointer
          // says it cannot be pressed.
          inFlight > 0
            ? "cursor-progress"
            : "disabled:cursor-default disabled:opacity-50",
        )}
      >
        {inFlight > 0 && (
          // Keyed on the fetch count so each re-read restarts the animation
          // from zero: the bar reaching the far edge and the list being read are
          // one event. The duration comes from the constant the poll uses, so
          // the two cannot drift.
          <span
            key={runs.dataUpdatedAt}
            aria-hidden
            className="pointer-events-none absolute inset-y-0 left-0 w-0 animate-[run-dwell_linear_forwards] bg-white/25 motion-reduce:animate-none"
            style={{ animationDuration: `${POLL_INTERVAL_MS}ms` }}
          />
        )}
        <span className="relative inline-flex items-center gap-1.5">
          {inFlight === 0 && !starting && (
            <Play className="h-3 w-3 fill-current" />
          )}
          {label}
        </span>
      </button>
    </div>
  );
}
