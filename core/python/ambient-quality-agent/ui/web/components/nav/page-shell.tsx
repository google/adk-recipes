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

import type { ReactNode } from "react";

import { SidebarRail } from "@/components/nav/sidebar-rail";
import { useLhaSessions } from "@/lib/horizon-sessions";

/** The left nav and the content column for every route that is not the chat.
 *
 * Renders the chat's own sidebar rather than a second one: the sidebar is the
 * only navigation there is, so it has to be the same object everywhere.
 *
 * Pages render their content and nothing around it; this scrolls it, centers
 * it and sizes it as a fraction of main above md (width="wide" is 80% for Home
 * and Investigations; width="narrow", the default, is 65% for every other
 * page) and full-width below it. The column is a flex column at least as tall
 * as the window, so a flex-1 child can fill what the content leaves, as the
 * loading pane does.
 *
 * activeContextId is null because no conversation is open here; that only
 * controls which row is highlighted.
 */
const COLUMN_WIDTH = {
  narrow: "w-full md:w-[65%]",
  wide: "w-full md:w-[80%]",
} as const;

export function PageShell({
  children,
  width = "narrow",
}: {
  children: ReactNode;
  width?: keyof typeof COLUMN_WIDTH;
}) {
  const { data: sessions, isLoading, refresh } = useLhaSessions();
  return (
    <SidebarRail
      activeContextId={null}
      sessions={sessions}
      sessionsLoading={isLoading}
      refreshSessions={refresh}
    >
      <main className="min-h-0 flex-1 overflow-auto motion-safe:animate-fade-in-up">
        <div
          className={`mx-auto flex min-h-full ${COLUMN_WIDTH[width]} flex-col p-6`}
        >
          {children}
        </div>
      </main>
    </SidebarRail>
  );
}
