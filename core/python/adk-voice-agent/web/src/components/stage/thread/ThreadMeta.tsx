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

import type { ThreadSummary } from "../../../lib/api";
import { plural } from "../../../lib/format";
import { statusName, threadTone } from "../../../lib/threads";

/** A thread's status dot and name, its turns (when more than one), and its age. */
export function ThreadMeta({
  thread,
  when,
}: {
  thread: ThreadSummary;
  /** Elapsed time while running, else how long ago it last moved. */
  when: string;
}) {
  const turns = Math.max(1, thread.turn_count || 1);
  return (
    <span className="thread-meta" data-tone={threadTone(thread)}>
      <span className="thread-meta-dot" aria-hidden />
      <span>{statusName(thread.status, thread.outcome)}</span>
      {turns > 1 && (
        <>
          <span aria-hidden>·</span>
          <span>{plural(turns, "turn")}</span>
        </>
      )}
      <span aria-hidden>·</span>
      <span>{when}</span>
    </span>
  );
}
