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

import type { InvestigationCounters } from "@/lib/aqua-api";
import { COUNTER_FIELDS } from "@/lib/counters";
import { textStyle } from "@/lib/typography";
import { cn } from "@/lib/utils";

export function CounterPanel({
  counters,
}: {
  counters?: InvestigationCounters;
}) {
  if (!counters) {
    return (
      <p className={cn(textStyle.meta, "p-3")}>
        No counters recorded for this investigation.
      </p>
    );
  }

  return (
    <div className="overflow-x-auto">
      <table className={cn(textStyle.meta, "w-full text-left")}>
        <tbody className="divide-y divide-border/40">
          {COUNTER_FIELDS.map((field) => (
            <tr
              key={field.key}
              title={field.help}
              className="transition-colors hover:bg-muted/40"
            >
              <td className="py-1.5 px-3">{field.label}</td>
              <td className="py-1.5 px-3 text-right font-medium text-foreground">
                {counters[field.key] ?? 0}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
