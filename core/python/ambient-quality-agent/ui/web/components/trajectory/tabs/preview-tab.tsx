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
import { Markdown } from "@/components/chat/markdown";
import { textStyle } from "@/lib/typography";
import { cn } from "@/lib/utils";

export function PreviewTab({ record }: { record: TrajectoryRecord }) {
  if (!record.text) {
    return <div className={cn(textStyle.meta, "p-3")}>No text content.</div>;
  }

  return (
    <div className={cn(textStyle.body, "p-3 overflow-x-auto")}>
      <Markdown text={record.text} />
    </div>
  );
}
