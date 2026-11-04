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

/** `trigger_type` of an investigation a person or the CLI asked for. Mirrors
 *  `MANUAL_TRIGGER_TYPE` in `investigations/models.py`; the ambient loop's own
 *  types are `scheduled`, `task_fire` and `update`. */
const MANUAL_TRIGGER = "manual";

/**
 * Where an investigation came from: the chat that started it, or the loop that
 * did.
 *
 * Ambient investigations have no chat conversation because the loop starts them
 * on a schedule or after an agent update. `trigger_type` distinguishes a
 * scheduled or update-triggered investigation (shown as `ambient`) from a
 * manual investigation whose chat was not recorded (shown as `No chat
 * recorded`).
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
