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

import type { TrajectoryRecord } from "@/lib/trajectory/types";
import { textStyle } from "@/lib/typography";
import { cn } from "@/lib/utils";

function prettyJson(val: unknown): string {
  if (val === undefined || val === null) return "";
  if (typeof val === "string") {
    try {
      return JSON.stringify(JSON.parse(val), null, 2);
    } catch {
      return val;
    }
  }
  return JSON.stringify(val, null, 2);
}

export function ToolsTab({ record }: { record: TrajectoryRecord }) {
  const args = prettyJson(record.args);
  const result = prettyJson(record.result);

  return (
    <div className="flex flex-col gap-3 p-3">
      <div className="flex items-center gap-2">
        <span className={cn(textStyle.code, "font-medium")}>
          {record.toolName || "(unnamed tool)"}
        </span>
        {record.isError && (
          <span
            className={cn(
              textStyle.label,
              "rounded border border-destructive bg-destructive/10 px-1.5 py-0.5 text-destructive",
            )}
          >
            Error
          </span>
        )}
      </div>

      {args && (
        <div className="flex flex-col gap-1">
          <span className={textStyle.label}>Arguments</span>
          <pre
            className={cn(
              textStyle.code,
              "overflow-x-auto rounded border bg-muted/40 p-2",
            )}
          >
            {args}
          </pre>
        </div>
      )}

      {result && (
        <div className="flex flex-col gap-1">
          <span className={textStyle.label}>Result</span>
          <pre
            className={cn(
              textStyle.code,
              "overflow-x-auto rounded border bg-muted/40 p-2",
              record.isError && "border-destructive/40 text-destructive",
            )}
          >
            {result}
          </pre>
        </div>
      )}
    </div>
  );
}
