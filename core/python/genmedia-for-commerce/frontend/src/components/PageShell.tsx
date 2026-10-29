/**
 * Copyright 2026 Google LLC
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     https://www.apache.org/licenses/LICENSE-2.0
 *
 * Unless required by applicable law or agreed to in writing, software
 * distributed under the License is distributed on an "AS IS" BASIS,
 * WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 * See the License for the specific language governing permissions and
 * limitations under the License.
 */

import type { ReactNode } from "react";
import TopNav from "./TopNav";

type PageShellProps = {
  title: string;
  subtitle: string;
  children: ReactNode;
  /** Grid columns of the children on large screens. */
  columns: string;
};

/** Page layout: top bar, title, then the children in a grid. */
export default function PageShell({
  title,
  subtitle,
  children,
  columns,
}: PageShellProps) {
  return (
    <div className="min-h-screen bg-gm-bg bg-page-glow bg-no-repeat">
      <TopNav />
      {/* Phones: room at the bottom for the floating chat button. */}
      <main className="px-4 sm:px-6 lg:px-8 pt-6 lg:pt-8 pb-28 lg:pb-10">
        <div className="max-w-[1440px] mx-auto">
          <header className="mb-6 lg:mb-8 animate-fade-in">
            <h1 className="text-h1 lg:text-[32px]">{title}</h1>
            <p className="mt-1 text-body-lg text-gm-text-secondary max-w-2xl">
              {subtitle}
            </p>
          </header>
          <div className={`grid gap-6 lg:gap-8 ${columns} items-start`}>
            {children}
          </div>
        </div>
      </main>
    </div>
  );
}
