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

import { stripMarkdownForSummary } from "@/lib/aqa-markdown";
import type { TrajectoryRecord } from "@/lib/trajectory/types";
import { mono, textStyle } from "@/lib/typography";
import { cn, formatSentenceCase } from "@/lib/utils";

function summaryOf(record: TrajectoryRecord): string {
  if (record.kind === "tool") {
    return record.toolName ? `${record.toolName}(...)` : "tool call";
  }
  return (
    stripMarkdownForSummary(record.text) ||
    (record.isError ? record.errorMessage || "Error" : "")
  );
}

const KIND_COLORS: Record<string, string> = {
  user: "bg-sky-500/10 text-sky-500 border-sky-500/30",
  assistant: "bg-emerald-500/10 text-emerald-500 border-emerald-500/30",
  tool: "bg-amber-500/10 text-amber-500 border-amber-500/30",
  system: "bg-muted text-muted-foreground border-border",
};

export function Row({
  record,
  selected,
  onSelect,
}: {
  record: TrajectoryRecord;
  selected: boolean;
  onSelect: (id: string) => void;
}) {
  return (
    // biome-ignore lint/a11y/useSemanticElements: a button is not full-width by default, so the swap would shrink the row to its content.
    <div
      className={cn(
        "flex items-center gap-2 rounded-md px-2.5 py-1.5 transition-colors cursor-pointer select-none",
        selected
          ? "bg-accent text-accent-foreground font-medium"
          : "hover:bg-muted/60 text-foreground",
        record.parentId && "ml-4 border-l-2 border-border/60 pl-2",
      )}
      onClick={() => onSelect(record.id)}
      data-testid="ledger-row"
      role="button"
      tabIndex={0}
      onKeyDown={(e) => {
        if (e.key === "Enter" || e.key === " ") {
          e.preventDefault();
          onSelect(record.id);
        }
      }}
    >
      <span className={cn(textStyle.meta, "shrink-0 w-5")}>
        #{record.index}
      </span>
      <span
        className={cn(
          textStyle.label,
          "rounded border px-1 py-0.2 shrink-0",
          KIND_COLORS[record.kind] || KIND_COLORS.system,
          record.isError &&
            "border-destructive text-destructive bg-destructive/10",
        )}
      >
        {formatSentenceCase(record.kind)}
      </span>
      <span
        className={cn(
          textStyle.meta,
          "truncate flex-1 text-current",
          record.kind === "tool" && mono,
        )}
      >
        {summaryOf(record)}
      </span>
    </div>
  );
}
