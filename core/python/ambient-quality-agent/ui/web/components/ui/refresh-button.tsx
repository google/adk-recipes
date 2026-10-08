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

import { RefreshCw } from "lucide-react";

import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";

/**
 * Re-read a list on demand.
 *
 * react-query holds the invalidation and refetches on its own schedule, which
 * is not the same as a reader being able to ask. Someone who has just started
 * a sweep elsewhere, or fixed a broken deployment, wants to know *now* and has
 * had no way to say so short of reloading the page.
 *
 * Disabled while the refetch is in flight, so a second click cannot stack
 * another one; the icon spins for the same period, which is the only signal
 * that anything happened when the data comes back unchanged.
 */
export function RefreshButton({
  onRefresh,
  refreshing,
  label = "Refresh the list",
  className,
}: {
  onRefresh: () => void;
  refreshing: boolean;
  label?: string;
  className?: string;
}) {
  return (
    <Button
      type="button"
      size="sm"
      variant="outline"
      disabled={refreshing}
      onClick={onRefresh}
      aria-label={label}
      title={label}
      className={cn("h-7 w-7 p-0", className)}
    >
      <RefreshCw
        className={cn(
          "h-3.5 w-3.5",
          // The spin is the feedback, so it is suppressed only where motion is
          // unwelcome -- there the disabled state carries it instead.
          refreshing && "animate-spin motion-reduce:animate-none",
        )}
        aria-hidden="true"
      />
    </Button>
  );
}
