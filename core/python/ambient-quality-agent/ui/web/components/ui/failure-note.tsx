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

import { Button } from "@/components/ui/button";
import { mono } from "@/lib/typography";
import { cn } from "@/lib/utils";
import { describeFailure } from "@/lib/failure";

/**
 * A failed read or a failed run, told in the reader's terms.
 *
 * One component for all three tones, because they differ only in what they
 * mean: an empty deployment is muted prose about what has not happened yet, an
 * unfinished one is amber prose about something still arriving, and a real
 * failure is a red alert about something that broke. Rendering the last where
 * either of the others was true is what made a project with no traces look
 * like a product that does not work.
 *
 * The raw text survives either way, under a disclosure. Whoever has to debug
 * the sink still needs the BigQuery message; they just should not be the only
 * reader it was written for.
 *
 * `lead` names what failed and is dropped when nothing did — "Couldn't load
 * the totals" is the wrong sentence for a deployment that has nothing to total
 * or no agent to total it yet. A page of working sections still needs its one
 * broken section named, which is why it is required rather than generic.
 *
 * Retry is offered where there is something to retry: a read that timed out or
 * could not reach the agent is worth asking for again, and without a control
 * the only way to ask was to reload the page.
 *
 * `compact` drops the explanation and the disclosure, for the secondary panes
 * of a page whose reads all fail together — one deployment with no telemetry
 * fails every read on the homepage at once, and four copies of the same
 * paragraph is its own kind of alarming.
 */
export function FailureNote({
  lead,
  error,
  onRetry,
  compact = false,
  className,
}: {
  lead: string;
  error: unknown;
  onRetry?: () => void;
  compact?: boolean;
  className?: string;
}) {
  const failure = describeFailure(error);

  if (failure.kind !== "error") {
    return (
      <div
        role="status"
        className={cn(
          "flex flex-col gap-1 text-sm",
          // Amber for a state that clears itself and is worth waiting out,
          // muted for one that is simply true. Neither is a fault, so neither
          // is red.
          failure.kind === "pending"
            ? "text-amber-500"
            : "text-muted-foreground",
          className,
        )}
      >
        <p className="flex flex-wrap items-center gap-2">
          <span>{failure.cause}</span>
          {failure.kind === "pending" && onRetry && (
            <Button
              size="sm"
              variant="outline"
              onClick={onRetry}
              className="h-6 text-xs"
            >
              Retry
            </Button>
          )}
        </p>
        {!compact && failure.hint && <p className="text-xs">{failure.hint}</p>}
        {!compact && <FailureDetail detail={failure.detail} />}
      </div>
    );
  }

  return (
    <div
      role="alert"
      className={cn("flex flex-col gap-1 text-sm text-destructive", className)}
    >
      <p className="flex flex-wrap items-center gap-2">
        <span>
          {lead}: {failure.cause}
        </span>
        {onRetry && (
          <Button
            size="sm"
            variant="outline"
            onClick={onRetry}
            className="h-6 text-xs"
          >
            Retry
          </Button>
        )}
      </p>
      {!compact && <FailureDetail detail={failure.detail} />}
    </div>
  );
}

/** The underlying message, collapsed. `<details>` rather than a toggle so it is
 *  keyboard-reachable and searchable once open, with no state to own. */
function FailureDetail({ detail }: { detail: string }) {
  if (!detail) return null;
  return (
    <details className="text-xs text-muted-foreground">
      <summary className="cursor-pointer select-none">Details</summary>
      <p className={cn("mt-1 whitespace-pre-wrap break-words", mono)}>
        {detail}
      </p>
    </details>
  );
}
