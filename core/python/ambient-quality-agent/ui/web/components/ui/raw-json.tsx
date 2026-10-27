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
import { textStyle } from "@/lib/typography";
import { cn } from "@/lib/utils";

/**
 * The record behind a view, as the agent actually sent it.
 *
 * Every rendered field is a choice about what matters, and the choice is
 * sometimes wrong — a field arrives that nothing displays, or a value looks
 * impossible and the question is whether the UI or the pipeline mangled it.
 * This is how that gets answered without a deploy, which is what it was worth
 * in the old dashboard and why all three of its uses are back.
 *
 * Collapsed by default, and *empty* until opened: a `<details>` keeps its
 * children in the DOM whatever it is showing, so serialising eagerly would put
 * a second copy of every string on the page — found by Ctrl-F, read out by a
 * screen reader, and matched by any test looking for text that is supposed to
 * appear once. It is an escape hatch, so it costs nothing until it is used.
 */
export function RawJson({ label, value }: { label: string; value: unknown }) {
  const [open, setOpen] = useState(false);
  return (
    <details
      onToggle={(e) => setOpen((e.currentTarget as HTMLDetailsElement).open)}
    >
      <summary className="cursor-pointer text-xs text-muted-foreground hover:text-foreground">
        {label}
      </summary>
      {open && (
        <pre
          className={cn(
            textStyle.code,
            "mt-2 max-h-96 overflow-auto rounded-md border bg-muted/40 p-3 whitespace-pre-wrap break-all",
          )}
        >
          {JSON.stringify(value, null, 2)}
        </pre>
      )}
    </details>
  );
}
