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

import { forwardRef } from "react";

import { cn } from "@/lib/utils";

interface FilterTabsProps<T> {
  /** The group's accessible name: "Status". */
  label: string;
  options: readonly T[];
  value: T;
  onSelect: (option: T) => void;
  labelOf: (option: T) => string;
}

/**
 * A row of toggle buttons that picks one view of a list, the pressed one
 * marked with `aria-pressed`.
 *
 * `onSelect` fires for the pressed tab too: a list that pages treats a click
 * on its own tab as "from the top".
 */
export const FilterTabs = forwardRef(function FilterTabs<T>(
  { label, options, value, onSelect, labelOf }: FilterTabsProps<T>,
  ref: React.ForwardedRef<HTMLDivElement>,
) {
  return (
    // biome-ignore lint/a11y/useSemanticElements: a fieldset brings UA min-inline-size into this flex row, and the ref type is part of the component's API.
    <div
      ref={ref}
      role="group"
      aria-label={label}
      className="flex items-center gap-1"
    >
      {options.map((option) => (
        <button
          key={labelOf(option)}
          type="button"
          aria-pressed={option === value}
          onClick={() => onSelect(option)}
          className={cn(
            "rounded-md px-2 py-0.5 text-xs transition-colors",
            option === value
              ? "bg-muted font-medium text-foreground"
              : "text-muted-foreground hover:text-foreground",
          )}
        >
          {labelOf(option)}
        </button>
      ))}
    </div>
  );
}) as <T>(
  // forwardRef drops the generic; this puts it back.
  props: FilterTabsProps<T> & { ref?: React.Ref<HTMLDivElement> },
) => React.ReactElement;
