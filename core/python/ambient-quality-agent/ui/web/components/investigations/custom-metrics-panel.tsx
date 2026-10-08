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

import type { MetricDetail, MetricOutcome } from "@/lib/aqua-api";
import { mono, textStyle } from "@/lib/typography";
import { cn } from "@/lib/utils";

/** Display labels for each metric execution kind or skip state. */
const KIND_LABEL: Record<string, string> = {
  local: "python · local",
  remote: "python · eval service",
  judged: "llm as judge · eval service",
  predefined: "predefined · not run",
  refused: "refused",
};

export function CustomMetricsPanel({
  outcomes,
  detail,
}: {
  outcomes?: Record<string, MetricOutcome>;
  detail?: Record<string, MetricDetail>;
}) {
  // Include detail keys so declined and refused metrics without outcomes
  // still render.
  const names = [
    ...new Set([...Object.keys(outcomes ?? {}), ...Object.keys(detail ?? {})]),
  ].sort();

  if (names.length === 0) {
    return (
      <p className={cn(textStyle.meta, "p-3")}>
        No custom metrics are published for this agent.
      </p>
    );
  }

  const hasPredefined = Object.values(detail ?? {}).some(
    (d) => d.kind === "predefined",
  );

  return (
    <div className="overflow-x-auto">
      <table className={cn(textStyle.meta, "w-full text-left")}>
        <thead className={cn(textStyle.label, "border-b border-border")}>
          <tr>
            <th className="py-2 px-3">Metric</th>
            <th className="py-2 px-3">Execution</th>
            <th className="py-2 px-3">Expects</th>
            <th className="py-2 px-3 text-right">Pass at</th>
            <th className="py-2 px-3 text-right">Passed</th>
            <th className="py-2 px-3 text-right">Failed</th>
            <th className="py-2 px-3 text-right">Eval service errors</th>
          </tr>
        </thead>
        <tbody className="divide-y divide-border/40">
          {names.map((name) => {
            const o = outcomes?.[name];
            const d = detail?.[name];
            const why = d?.not_run;
            // Leave blank rather than defaulting to local so missing metadata
            // never fabricates an execution mode.
            const kind = d?.kind ?? "";
            return (
              <tr key={name} className={cn("align-top", why && "opacity-70")}>
                <td className="py-2 px-3 font-medium text-foreground">
                  {name}
                  {d?.model_modules && d.model_modules.length > 0 && (
                    <span className="ml-2 text-amber-500">calls a model</span>
                  )}
                </td>
                <td className="py-2 px-3 text-muted-foreground">
                  <span>{kind in KIND_LABEL ? KIND_LABEL[kind] : kind}</span>
                  {why && <span className="block">{why}</span>}
                </td>
                <td className="py-2 px-3 text-muted-foreground">
                  {d?.expected || "—"}
                </td>
                <td className="py-2 px-3 text-right text-muted-foreground">
                  {d?.scores
                    ? `scores only · mean ${d.scores.mean.toFixed(2)}`
                    : (d?.threshold ?? "—")}
                </td>
                <td className="py-2 px-3 text-right text-emerald-500">
                  {o ? o.passed : "—"}
                </td>
                <td
                  className={cn(
                    "py-2 px-3 text-right",
                    o && o.failed > 0
                      ? "text-orange-400"
                      : "text-muted-foreground",
                  )}
                >
                  {o ? o.failed : "—"}
                </td>
                <td
                  className={cn(
                    "py-2 px-3 text-right",
                    o && o.errored > 0
                      ? "text-destructive"
                      : "text-muted-foreground",
                  )}
                >
                  {o ? o.errored : "—"}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
      {hasPredefined && (
        <p className={cn(textStyle.meta, "px-3 pt-2")}>
          Predefined metrics run from deployment configuration using{" "}
          <code className={mono}>MULTI_TURN_METRICS</code> and{" "}
          <code className={mono}>SINGLE_TURN_METRICS</code> rather than the
          published library, so matching entries are skipped here.
        </p>
      )}
    </div>
  );
}
