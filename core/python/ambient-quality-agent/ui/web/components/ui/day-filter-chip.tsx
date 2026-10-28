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

import { X } from "lucide-react";

/**
 * The day a list is narrowed to, as one button that removes it.
 *
 * One target rather than a label with a small ×, and drawn as a rounded,
 * outlined chip so it cannot be read as another of the tabs beside it. The day
 * is UTC because the per-day charts bucket by UTC day, while the rows below it
 * print their times in the reader's zone; saying so is what stops a run at
 * 23:00 local from looking misfiled.
 *
 * The accessible name starts with the visible text, so a voice-control user
 * can say what they see (WCAG 2.5.3), and then says what pressing it does.
 *
 * Removing the day unmounts this button, so the caller moves focus somewhere
 * that stays.
 */
export function DayFilterChip({
  day,
  onClear,
}: {
  day: string;
  onClear: () => void;
}) {
  return (
    <button
      type="button"
      onClick={onClear}
      aria-label={`Day ${day} (UTC), remove filter`}
      title="Show every day"
      className="inline-flex h-6 items-center gap-1 rounded-full border border-primary/30 bg-primary/5 px-2 text-xs text-primary hover:bg-primary/10 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
    >
      Day {day} (UTC)
      <X className="h-3.5 w-3.5" aria-hidden="true" />
    </button>
  );
}
