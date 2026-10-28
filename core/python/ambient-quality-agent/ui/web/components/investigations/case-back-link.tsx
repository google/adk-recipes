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
import { ArrowLeft } from "lucide-react";
import { textStyle } from "@/lib/typography";
import { cn } from "@/lib/utils";

/** Run segment a preview link carries in place of a run id. Mirrors
 *  `PREVIEW_RUN_SEGMENT` in `core/investigation/preview.py`, which builds the
 *  case-view links the chat agent hands out; the two must agree. */
export const PREVIEW_RUN_SEGMENT = "preview";

/**
 * Where the case view goes back to: the run that produced the case, or the
 * chat that previewed it.
 *
 * A preview link has no run behind it, so pointing back at
 * `/investigations/preview` would error. `/c` with no `?id=` restores the last
 * conversation from local storage, which is the chat the preview was opened
 * from — previews open in a new tab of the same browser.
 */
export function CaseBackLink({ runId }: { runId: string }) {
  const className = cn(
    textStyle.meta,
    "inline-flex w-fit items-center gap-1 hover:text-foreground",
  );

  if (runId === PREVIEW_RUN_SEGMENT) {
    return (
      <Link to="/c" className={className}>
        <ArrowLeft className="h-3 w-3" /> Back to chat
      </Link>
    );
  }

  return (
    <Link to="/investigations/$runId" params={{ runId }} className={className}>
      <ArrowLeft className="h-3 w-3" /> Run {runId.slice(0, 8)}
    </Link>
  );
}
