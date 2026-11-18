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

import type { InvestigationProgress } from "@/lib/aqua-api";
import { count } from "@/lib/funnel";
import { describeProgress } from "@/lib/investigation-summary";
import { textStyle } from "@/lib/typography";
import { cn } from "@/lib/utils";

/** How far a running investigation got, as a bar. The caller sizes it. Its
 *  fill is neutral: an accent skin's primary can be orange or green, which
 *  the pass-rate bars beside it use for failed and passed.
 *
 * A sweep's bar is filled to the share of its trajectories judged, glides to
 * a new share when the list is re-read, and has a highlight crossing it, since
 * the share itself moves only every few minutes. A stage with no total to
 * measure against (clustering, verification) gets a segment travelling the
 * track instead, drawn only while it can move: a still bar would read as
 * empty. Motion stops where the user asks for reduced motion.
 */
export function ProgressBar({
  progress,
  className,
}: {
  progress: InvestigationProgress;
  className?: string;
}) {
  const total = progress.total ?? 0;
  if (total <= 0) {
    return (
      <span
        role="progressbar"
        aria-label={progress.stage}
        aria-valuetext={describeProgress(progress) || undefined}
        className={cn(
          "hidden overflow-hidden rounded-full bg-muted-foreground/20 motion-safe:block",
          className,
        )}
      >
        <span className="block h-full w-1/4 rounded-full bg-muted-foreground motion-safe:animate-progress-travel" />
      </span>
    );
  }
  const done = Math.min(count(progress.evaluated), total);
  return (
    <span
      role="progressbar"
      aria-label={progress.stage}
      aria-valuemin={0}
      aria-valuemax={total}
      aria-valuenow={done}
      aria-valuetext={describeProgress(progress) || undefined}
      className={cn(
        "block overflow-hidden rounded-full bg-muted-foreground/20",
        className,
      )}
    >
      <span
        className="relative block h-full overflow-hidden rounded-full bg-muted-foreground transition-[width] duration-700 ease-out motion-reduce:transition-none"
        style={{ width: `${(done / total) * 100}%` }}
      >
        <span
          aria-hidden="true"
          className="absolute inset-0 hidden bg-gradient-to-r from-transparent via-white/40 to-transparent motion-safe:block motion-safe:animate-progress-sheen"
        />
      </span>
    </span>
  );
}

/** A running investigation's stage, its counts so far and its bar, for where
 *  its funnel will be once it finishes. */
export function InvestigationProgressPanel({
  progress,
}: {
  progress: InvestigationProgress;
}) {
  return (
    <div className="flex flex-col gap-2">
      <div className="flex flex-wrap items-baseline justify-between gap-x-3">
        <span className={textStyle.body}>{progress.stage}</span>
        <span className={textStyle.meta}>{describeProgress(progress)}</span>
      </div>
      <ProgressBar progress={progress} className="h-2 w-full" />
      <p className={textStyle.meta}>
        The full counts are recorded when the investigation finishes.
      </p>
    </div>
  );
}
