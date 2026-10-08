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

import type { InsightView } from "@/lib/aqua-api";
import { Button } from "@/components/ui/button";
import { textStyle } from "@/lib/typography";
import { cn } from "@/lib/utils";

/** Shown while `InsightsView` is in merge-picking mode. Picking a survivor is
 * a separate, explicit step from selecting duplicates -- merging two things
 * "into whichever happened to be clicked first" is how a real diagnosis
 * accidentally gets folded into an unverified guess. */
export function MergeBar({
  insights,
  selected,
  onCancel,
  onMerge,
  merging,
}: {
  insights: readonly InsightView[];
  selected: ReadonlySet<string>;
  onCancel: () => void;
  onMerge: (targetInsightId: string) => void;
  merging: boolean;
}) {
  const selectedInsights = insights.filter((i) => selected.has(i.insight_id));
  const canMerge = selectedInsights.length >= 2;

  return (
    <section
      aria-label="Merge duplicate insights"
      className="flex flex-col gap-3 rounded-lg border border-accent/40 bg-card p-4"
    >
      <div className="flex items-start justify-between gap-2">
        <p className={textStyle.meta}>
          Select two or more cards that represent the same underlying insight,
          then pick which diagnosis survives. The others are marked as
          duplicates of it.
        </p>
        <Button
          size="sm"
          variant="ghost"
          onClick={onCancel}
          disabled={merging}
          className="h-7"
        >
          Cancel
        </Button>
      </div>

      {selectedInsights.length > 0 && (
        <div className="flex flex-col gap-1.5">
          <span className={cn(textStyle.label, "text-foreground")}>
            {canMerge
              ? "Choose which diagnosis to keep:"
              : "Select at least one more to merge:"}
          </span>
          <ul className="flex flex-col gap-1">
            {selectedInsights.map((insight) => (
              <li key={insight.insight_id}>
                <Button
                  size="sm"
                  variant="outline"
                  disabled={!canMerge || merging}
                  onClick={() => onMerge(insight.insight_id)}
                  className="h-auto w-full justify-start py-2 text-left font-normal"
                >
                  <span className="font-medium text-primary mr-1.5">Keep:</span>{" "}
                  <span className="truncate">
                    {insight.diagnosis || insight.label}
                  </span>
                </Button>
              </li>
            ))}
          </ul>
        </div>
      )}
    </section>
  );
}
