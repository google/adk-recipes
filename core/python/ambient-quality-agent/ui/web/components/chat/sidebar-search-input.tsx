"use client";
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

import { Search } from "lucide-react";
import { textStyle } from "@/lib/typography";
import { cn } from "@/lib/utils";

interface SidebarSearchInputProps {
  value: string;
  onChange: (value: string) => void;
  placeholder: string;
  ariaLabel: string;
}

export function SidebarSearchInput({
  value,
  onChange,
  placeholder,
  ariaLabel,
}: SidebarSearchInputProps) {
  return (
    <div className="relative">
      <Search
        aria-hidden="true"
        className="pointer-events-none absolute left-2 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-muted-foreground"
      />
      <input
        type="search"
        value={value}
        onChange={(e) => onChange(e.target.value)}
        aria-label={ariaLabel}
        placeholder={placeholder}
        className={cn(
          textStyle.meta,
          "h-7 w-full rounded-md border border-input bg-background pl-7 pr-2 text-foreground outline-none placeholder:text-muted-foreground focus-visible:ring-2 focus-visible:ring-ring",
        )}
      />
    </div>
  );
}
