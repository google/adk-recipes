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

export function RawTab({ record }: { record: TrajectoryRecord }) {
  const data =
    record.raw &&
    typeof record.raw === "object" &&
    Object.keys(record.raw).length > 0
      ? record.raw
      : record;
  const json = JSON.stringify(data, null, 2);

  return (
    <div className="p-3">
      <pre
        className={cn(
          textStyle.code,
          "overflow-x-auto rounded border bg-muted/40 p-2",
        )}
      >
        {json}
      </pre>
    </div>
  );
}
