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

import { useId, type ReactNode } from "react";
import type { HorizonSessionSummary } from "@/lib/horizon-sessions";
import { textStyle } from "@/lib/typography";
import { cn } from "@/lib/utils";
import { ChatRow } from "./chat-row";

interface SessionGroup {
  label: string;
  sessions: HorizonSessionSummary[];
}

interface SidebarSessionGroupsProps {
  groups: SessionGroup[];
  activeContextId: string | null;
  onSelect: (id: string) => void;
  onAfterRename: () => void;
  onAfterDelete: (id: string) => void;
  onPrefetch?: (id: string) => void;
  footer?: ReactNode;
}

export function SidebarSessionGroups({
  groups,
  activeContextId,
  onSelect,
  onAfterRename,
  onAfterDelete,
  onPrefetch,
  footer,
}: SidebarSessionGroupsProps) {
  const baseId = useId();
  return (
    <div className="flex flex-col gap-2">
      {groups.map((group, i) => (
        <section key={group.label} className="flex flex-col">
          {/* Sticky so a label never sits orphaned at the bottom edge with
              none of its rows showing. Painted with the rail's own colour
              (card at 30% over the page) so rows scroll under it unseen. */}
          <h3
            id={`${baseId}-${i}`}
            className={cn(
              textStyle.label,
              "sticky top-0 z-10 px-2 py-1 [background:linear-gradient(hsl(var(--card)/0.3),hsl(var(--card)/0.3)),hsl(var(--background))]",
            )}
          >
            {group.label}
          </h3>
          <ul
            aria-labelledby={`${baseId}-${i}`}
            className="flex flex-col gap-0.5"
          >
            {group.sessions.map((s) => (
              <ChatRow
                key={s.id}
                session={s}
                active={s.id === activeContextId}
                onSelect={onSelect}
                onAfterRename={onAfterRename}
                onAfterDelete={onAfterDelete}
                onPrefetch={onPrefetch}
              />
            ))}
          </ul>
        </section>
      ))}
      {footer}
    </div>
  );
}
