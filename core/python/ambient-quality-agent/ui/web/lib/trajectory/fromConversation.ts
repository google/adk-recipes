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

import type { CaseConversation } from "../aqua-api";
import { timelineOf } from "../conversation";
import type { TrajectoryDoc, TrajectoryRecord } from "./types";

export function fromConversation(convo: CaseConversation): TrajectoryDoc {
  const records: TrajectoryRecord[] = [];
  let index = 0;

  for (const item of timelineOf(convo)) {
    if (item.kind === "text") {
      records.push({
        id: `turn-${item.turnIndex}-${index}`,
        index: index++,
        kind: item.author === "user" ? "user" : "assistant",
        turn: item.turnIndex,
        agent: item.author ?? "unknown",
        text: item.text,
        raw: item,
      });
    } else if (item.kind === "emptyResponse") {
      records.push({
        id: `empty-${item.turnIndex}-${index}`,
        index: index++,
        kind: "assistant",
        turn: item.turnIndex,
        agent: item.author ?? "unknown",
        text: "(no response)",
        raw: item,
      });
    } else {
      for (const call of item.calls) {
        records.push({
          id: `call-${call.callId ?? `${call.turnIndex}-${index}`}`,
          index: index++,
          kind: "tool",
          turn: call.turnIndex,
          agent: "tool",
          text: "",
          toolName: call.toolName ?? undefined,
          args: call.args,
          result: call.response,
          isError: call.isError,
          raw: call,
        });
      }
    }
  }

  return { records, timingRecorded: false };
}
