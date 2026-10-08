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

import { useState } from "react";
import { Info } from "lucide-react";
import { Popover, PopoverContent, PopoverTrigger } from "./popover";

/**
 * An (i) button that shows a short explanation of `topic`.
 *
 * The button is named "About <topic>", so `topic` must tell it apart from
 * every other (i) on the page: "the Insights stage", not "Insights".
 *
 * A popover rather than a tooltip: a tooltip opens on hover and focus but never
 * on a tap, so a touch reader could not reach the text at all.
 *
 * A toggletip to assistive tech: focus stays on the button, so the text is
 * spoken through a status region beside it, and the box itself is hidden
 * from assistive tech so the text is not read twice.
 */
export function HelpPopover({
  topic,
  children,
}: {
  topic: string;
  children: React.ReactNode;
}) {
  const name = `About ${topic}`;
  const [open, setOpen] = useState(false);
  return (
    // Positioned so the `sr-only` status region below is placed against this
    // wrapper: with no positioned parent, one beside a chart far down Home was
    // placed against the page and made the whole document scroll.
    <span className="relative inline-flex shrink-0 items-center">
      <Popover open={open} onOpenChange={setOpen}>
        <PopoverTrigger
          aria-label={name}
          className="-my-1 inline-grid h-6 w-6 shrink-0 place-items-center rounded-full text-muted-foreground hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-inset"
        >
          <Info className="h-3.5 w-3.5" aria-hidden="true" />
        </PopoverTrigger>
        {/* Focus stays on the button: the text holds nothing to focus, and
            focus moved into it would leave Tab with nowhere to go. Tab moves
            on and closes it; Escape closes it. */}
        <PopoverContent
          aria-hidden="true"
          aria-label={name}
          align="start"
          collisionPadding={8}
          onOpenAutoFocus={(event) => event.preventDefault()}
          className="max-w-xs text-xs"
        >
          {children}
        </PopoverContent>
      </Popover>
      {/* Always rendered, and filled on open: a live region speaks when its
          content changes, not when it is added with content already in it. */}
      <span role="status" aria-live="polite" className="sr-only">
        {open ? children : null}
      </span>
    </span>
  );
}
