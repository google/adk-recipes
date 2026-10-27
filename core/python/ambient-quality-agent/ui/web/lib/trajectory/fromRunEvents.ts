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

import type { InvestigationEvent } from "../aqua-api";
import { blocks } from "../aqa-markdown";
import type { TrajectoryDoc, TrajectoryRecord } from "./types";

export function fromRunEvents(
  events: readonly InvestigationEvent[],
): TrajectoryDoc {
  const records: TrajectoryRecord[] = [];
  let index = 0;

  for (let turn = 0; turn < events.length; turn++) {
    const event = events[turn];
    const parentId = `event-${turn}-${index}`;

    records.push({
      id: parentId,
      index: index++,
      kind: "system",
      turn,
      agent: event.source ?? "workflow",
      text: event.text.trim() || event.source || `step ${turn + 1}`,
      raw: event,
    });

    for (const block of blocks(event.text)) {
      if (block.t === "fence" && block.lang === "python") {
        records.push({
          id: `code-${turn}-${index}`,
          index: index++,
          kind: "tool",
          turn,
          agent: "sandbox",
          text: "",
          parentId,
          toolName: "execute_code",
          args: { code: block.code },
          raw: block,
        });
      } else if (block.t === "list" && block.fence) {
        records.push({
          id: `code-${turn}-${index}`,
          index: index++,
          kind: "tool",
          turn,
          agent: "sandbox",
          text: "",
          parentId,
          toolName: "execute_code",
          args: { code: block.fence.code },
          raw: block,
        });
      }
    }
  }

  return { records, timingRecorded: false };
}
