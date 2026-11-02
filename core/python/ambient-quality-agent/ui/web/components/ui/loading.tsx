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
import { Loader2 } from "lucide-react";

import { cn } from "@/lib/utils";

/**
 * A spinner with its label.
 *
 * Reads through the agent, so a first paint is a couple of seconds and a bare
 * "Loading…" looks indistinguishable from a page that has given up. `Loader2`
 * with `animate-spin` is the idiom the rest of the fork already uses.
 */
export function Loading({
  label = "Loading…",
  className,
}: {
  label?: string;
  className?: string;
}) {
  return (
    <span
      className={cn(
        "inline-flex items-center gap-2 text-sm text-muted-foreground",
        className,
      )}
      role="status"
      aria-live="polite"
    >
      <Loader2 className="h-4 w-4 shrink-0 animate-spin" aria-hidden="true" />
      {label}
    </span>
  );
}

/** `Loading` filling the pane it is given, for a whole-route first paint. */
export function LoadingPane({ label }: { label?: string }) {
  return (
    <div className="flex h-full flex-1 items-center justify-center p-6">
      <Loading label={label} />
    </div>
  );
}
