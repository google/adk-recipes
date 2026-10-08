/**
 * Copyright 2026 Google LLC
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 *
 * Unless required by applicable law or agreed to in writing, software
 * distributed under the License is distributed on an "AS IS" BASIS,
 * WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 * See the License for the specific language governing permissions and
 * limitations under the License.
 */

import { Link } from "@tanstack/react-router";
import { MessagesSquare } from "lucide-react";
import { link, textStyle } from "@/lib/typography";
import { cn } from "@/lib/utils";

/** `trigger_type` of a run a person or the CLI asked for. Mirrors
 *  `MANUAL_TRIGGER_TYPE` in `investigations/models.py`; the ambient loop's own
 *  types are `scheduled`, `task_fire` and `update`. */
const MANUAL_TRIGGER = "manual";

/**
 * Where a run came from: the chat that started it, or the loop that did.
 *
 * Most runs have no conversation and never will — the ambient loop starts them
 * on a schedule, so a missing chat is not a gap in the record. Saying "no chat
 * recorded" of those, as this used to for every run, describes a deployment
 * working exactly as designed as though something were missing from it.
 *
 * `trigger_type` is what separates the second case from the third. Without it,
 * a manual run whose chat went unrecorded and a scheduled run that never had
 * one look identical, and calling both "ambient" would be false of the first.
 */
export function ConversationLink({
  contextId,
  triggerType,
}: {
  contextId?: string | null;
  triggerType?: string | null;
}) {
  if (contextId) {
    return (
      <Link
        to="/c"
        search={{ id: contextId }}
        className={cn(
          link.standalone,
          "inline-flex items-center gap-1 text-primary",
        )}
      >
        <MessagesSquare className="h-3 w-3" />
        Open the chat that started this
      </Link>
    );
  }

  if (triggerType && triggerType !== MANUAL_TRIGGER) {
    return (
      <span
        className={cn(textStyle.meta, "rounded-full border px-2 py-0.5")}
        title={`Started by the ambient loop (${triggerType}), not from a chat.`}
      >
        ambient
      </span>
    );
  }

  // Asked for by a person but nothing recorded where from, or a record too old
  // to say which. That one really is a gap, so it still reads as one.
  return (
    <span
      className="text-muted-foreground"
      title="This investigation was not started from a chat, or the chat was not recorded."
    >
      No chat recorded
    </span>
  );
}
