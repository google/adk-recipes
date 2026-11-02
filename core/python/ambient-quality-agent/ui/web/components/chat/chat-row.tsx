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

import { memo, useCallback, useEffect, useRef } from "react";
import { useNow } from "@/lib/now-context";
import { textStyle } from "@/lib/typography";
import { cn } from "@/lib/utils";
import type { HorizonSessionSummary } from "@/lib/horizon-sessions";
import {
  ChatActionButtons,
  ChatDeleteError,
  ChatTitleInput,
  useChatTitleControls,
} from "./chat-title-controls";

export const DAY_MS = 86_400_000;

export function formatRelative(ts: number, now: number): string {
  const diff = now - ts;
  if (diff < 60_000) return "now";
  if (diff < 3_600_000) return `${Math.floor(diff / 60_000)}m`;
  if (diff < DAY_MS) return `${Math.floor(diff / 3_600_000)}h`;
  if (diff < 2 * DAY_MS) return "1d";
  if (diff < 7 * DAY_MS) return `${Math.floor(diff / DAY_MS)}d`;
  return new Date(ts).toLocaleDateString(undefined, {
    month: "short",
    day: "numeric",
  });
}

export interface ChatRowProps {
  session: HorizonSessionSummary;
  active: boolean;
  onSelect: (id: string) => void;
  onAfterRename: () => void;
  onAfterDelete: (id: string) => void;
  // Warm this chat's history after a brief hover so opening it feels instant.
  onPrefetch?: (id: string) => void;
}

// Hover this long before prefetching so cursoring past rows doesn't fire a
// request for every chat the pointer crosses.
const PREFETCH_HOVER_MS = 150;

export const ChatRow = memo(function ChatRow({
  session,
  active,
  onSelect,
  onAfterRename,
  onAfterDelete,
  onPrefetch,
}: ChatRowProps) {
  const now = useNow();
  // Old clients and hand-edited storage can leave a blank title; a blank row
  // would also collapse to the timestamp's height.
  const title = session.title.trim() || "Untitled chat";
  const hoverTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

  const cancelHover = useCallback(() => {
    if (hoverTimer.current) {
      clearTimeout(hoverTimer.current);
      hoverTimer.current = null;
    }
  }, []);

  const handleMouseEnter = useCallback(() => {
    if (active || !onPrefetch) return;
    cancelHover();
    hoverTimer.current = setTimeout(
      () => onPrefetch(session.id),
      PREFETCH_HOVER_MS,
    );
  }, [active, onPrefetch, cancelHover, session.id]);

  useEffect(() => cancelHover, [cancelHover]);

  const handleAfterDelete = useCallback(
    () => onAfterDelete(session.id),
    [onAfterDelete, session.id],
  );
  const controls = useChatTitleControls({
    sessionId: session.id,
    title: session.title,
    onAfterRename,
    onAfterDelete: handleAfterDelete,
  });

  // The rename input replaces the row, so ending a rename drops focus to the
  // page. Put it back on the row, unless the user already moved it elsewhere.
  const rowRef = useRef<HTMLButtonElement>(null);
  const wasEditing = useRef(false);
  useEffect(() => {
    const ended = wasEditing.current && !controls.editing;
    wasEditing.current = controls.editing;
    if (!ended) return;
    const focused = document.activeElement;
    if (!focused || focused === document.body) rowRef.current?.focus();
  }, [controls.editing]);

  return (
    <li>
      {controls.editing ? (
        // px-0.5 plus the input's border and padding keeps the draft's first
        // letter on the title's left edge.
        <div className="flex min-h-8 items-center rounded-md bg-accent/40 px-0.5 py-1">
          <ChatTitleInput
            controls={controls}
            className={cn(textStyle.body, "h-6 px-1.5 py-0")}
          />
        </div>
      ) : (
        // The row and its actions are siblings, not nested: a button inside a
        // button is invalid, and keys pressed on an action would reach the row.
        <div className="group relative">
          <button
            ref={rowRef}
            type="button"
            aria-current={active ? "page" : undefined}
            onClick={() => onSelect(session.id)}
            onMouseEnter={handleMouseEnter}
            onMouseLeave={cancelHover}
            onDoubleClick={controls.startEditing}
            className={cn(
              textStyle.body,
              "flex min-h-8 w-full items-center rounded-md px-2 py-1.5 text-start transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-ring",
              active ? "bg-accent font-medium" : "hover:bg-accent",
            )}
          >
            {/* pr reserves the timestamp's slot, and on mobile the always-visible actions too. */}
            {/* dir="auto" so an RTL title keeps its opening words and truncates at its own end. */}
            <span
              dir="auto"
              className="min-w-0 flex-1 truncate pr-12 max-md:pr-24"
              title={title}
            >
              {title}
            </span>
            <span
              className={cn(
                textStyle.meta,
                "absolute right-2 top-1/2 -translate-y-1/2 transition-opacity group-focus-within:opacity-0 group-hover:opacity-0 max-md:right-[3.75rem] max-md:opacity-100 max-md:group-focus-within:opacity-100 max-md:group-hover:opacity-100",
              )}
            >
              {formatRelative(session.lastUpdated, now)}
            </span>
          </button>
          <div className="pointer-events-none absolute right-1.5 top-1/2 flex -translate-y-1/2 items-center gap-0.5 opacity-0 transition-opacity group-focus-within:pointer-events-auto group-focus-within:opacity-100 group-hover:pointer-events-auto group-hover:opacity-100 max-md:pointer-events-auto max-md:opacity-100">
            <ChatActionButtons title={title} controls={controls} />
          </div>
        </div>
      )}
      <ChatDeleteError error={controls.deleteError} className="mx-2 mt-1" />
    </li>
  );
});
