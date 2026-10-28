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

import type { HTMLAttributes, ReactNode } from "react";
import { textStyle } from "@/lib/typography";
import { cn } from "@/lib/utils";

/**
 * A page's header: its one `h1`, set as pageTitle, with an optional description
 * under it. `aside` sits on the title's line, at its end; `children` follow the
 * description. Every dashboard page renders its title through this component.
 */
export function PageHeader({
  title,
  description,
  aside,
  children,
  className,
  ...rest
}: {
  title: ReactNode;
  description?: ReactNode;
  aside?: ReactNode;
  children?: ReactNode;
} & Omit<HTMLAttributes<HTMLElement>, "title">) {
  return (
    <header className={cn("flex flex-col gap-1", className)} {...rest}>
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <h1 className={cn(textStyle.pageTitle, "break-words")}>{title}</h1>
        {aside}
      </div>
      {description && <p className={textStyle.description}>{description}</p>}
      {children}
    </header>
  );
}
