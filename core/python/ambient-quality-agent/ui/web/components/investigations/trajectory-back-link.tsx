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

/** Investigation segment a preview link carries in place of an investigation
 *  id. Mirrors `PREVIEW_INVESTIGATION_SEGMENT` in
 *  `core/investigation/preview.py`, which builds the trajectory-view links the
 *  chat agent hands out; the two must agree. */
export const PREVIEW_INVESTIGATION_SEGMENT = "preview";

/**
 * Where the trajectory view goes back to: the investigation that sampled the
 * trajectory, or the chat that previewed it.
 *
 * A preview link has no investigation behind it, so pointing back at
 * `/investigations/preview` would error. `/c` with no `?id=` restores the last
 * conversation from local storage, which is the chat the preview was opened
 * from — previews open in a new tab of the same browser.
 */
export function TrajectoryBackLink({
  investigationId,
}: {
  investigationId: string;
}) {
  const className = cn(
    textStyle.meta,
    "inline-flex w-fit items-center gap-1 hover:text-foreground",
  );

  if (investigationId === PREVIEW_INVESTIGATION_SEGMENT) {
    return (
      <Link to="/c" className={className}>
        <ArrowLeft className="h-3 w-3" /> Back to chat
      </Link>
    );
  }

  return (
    <Link
      to="/investigations/$investigationId"
      params={{ investigationId }}
      className={className}
    >
      <ArrowLeft className="h-3 w-3" /> Investigation{" "}
      {investigationId.slice(0, 8)}
    </Link>
  );
}
