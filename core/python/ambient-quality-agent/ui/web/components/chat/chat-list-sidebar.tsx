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

import { memo, useCallback, useMemo, useState } from "react";
import { Link, useNavigate } from "@tanstack/react-router";
import { MessageSquare, PanelLeftClose, Settings, Trash2 } from "lucide-react";

import { Button } from "@/components/ui/button";
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from "@/components/ui/alert-dialog";
import {
  deleteAllLhaSessions,
  useLhaSessions,
  type HorizonSessionSummary,
} from "@/lib/horizon-sessions";
import { TopIssuesRail } from "@/components/insights/top-issues-rail";
import { RecentRunsRail } from "@/components/investigations/recent-runs-rail";
import { textStyle } from "@/lib/typography";
import { forgetLastChat } from "@/lib/last-chat";
import { cn } from "@/lib/utils";
import { DAY_MS } from "./chat-row";
import { NewConversationMenu } from "./new-conversation-menu";
import { SidebarSection } from "./sidebar-section";
import { SidebarSearchInput } from "./sidebar-search-input";
import { SidebarSessionGroups } from "./sidebar-session-groups";
import {
  SIDEBAR_ROW_ACTIVE_CLASS,
  SIDEBAR_ROW_CLASS,
  SidebarNote,
  SidebarSkeleton,
} from "./sidebar-states";
import { RAIL_SCROLL_CLASS, useRailScroll } from "./use-rail-scroll";

// Every route renders its own rail, so the chat search would reset on each
// navigation, including the one a search result starts. It lives here instead.
let savedChatQuery = "";

/** Clears the remembered chat search. For tests, which share the module. */
export function resetChatSearchForTest(): void {
  savedChatQuery = "";
}

interface ChatListSidebarProps {
  activeContextId: string | null;
  sessions: HorizonSessionSummary[] | undefined;
  sessionsLoading: boolean;
  refreshSessions: () => Promise<unknown>;
  onNavigate?: () => void;
  // Called instead of router.push("/") so the parent can reset in-page
  // state when we're already on the home route (no nav → no remount).
  onNewChat?: () => void;
  // Warm a chat's history on hover so opening it is instant.
  onPrefetch?: (id: string) => void;
  // When true, reserve room on the first section header for a sheet close
  // button (mobile drawer rendering).
  reserveHeaderRight?: boolean;
  // Hide the rail. Absent on mobile, where the drawer has its own close.
  onCollapse?: () => void;
}

interface SessionGroup {
  label: string;
  sessions: HorizonSessionSummary[];
}

function groupSessions(sessions: HorizonSessionSummary[]): SessionGroup[] {
  const now = new Date();
  const todayStart = new Date(
    now.getFullYear(),
    now.getMonth(),
    now.getDate(),
  ).getTime();
  const yesterdayStart = todayStart - DAY_MS;
  const week = todayStart - 7 * DAY_MS;
  const month = todayStart - 30 * DAY_MS;

  const buckets: Record<string, HorizonSessionSummary[]> = {
    Today: [],
    Yesterday: [],
    "Last 7 days": [],
    "Last 30 days": [],
    Older: [],
  };

  const sorted = [...sessions].sort((a, b) => b.lastUpdated - a.lastUpdated);
  for (const s of sorted) {
    const ts = s.lastUpdated;
    if (ts >= todayStart) buckets.Today.push(s);
    else if (ts >= yesterdayStart) buckets.Yesterday.push(s);
    else if (ts >= week) buckets["Last 7 days"].push(s);
    else if (ts >= month) buckets["Last 30 days"].push(s);
    else buckets.Older.push(s);
  }

  return Object.entries(buckets)
    .filter(([, list]) => list.length > 0)
    .map(([label, list]) => ({ label, sessions: list }));
}

function ChatListSidebarInner({
  activeContextId,
  sessions,
  sessionsLoading,
  refreshSessions,
  onCollapse,
  onNavigate,
  onNewChat,
  onPrefetch,
  reserveHeaderRight,
}: ChatListSidebarProps) {
  const navigate = useNavigate();

  // Chat search filters the user's stored conversations; an empty query reuses
  // the parent's list (same query key).
  const [chatQuery, setChatQueryState] = useState(() => savedChatQuery);
  const setChatQuery = useCallback((q: string) => {
    savedChatQuery = q;
    setChatQueryState(q);
  }, []);
  const searching = chatQuery.trim().length > 0;
  const { data: searchData, isLoading: searchLoading } =
    useLhaSessions(chatQuery);
  const chatList = searching ? searchData : sessions;
  const chatLoading = searching ? searchLoading : sessionsLoading;
  // No search box over nothing: it appears once there is a chat to find.
  const showSearch = searching || (sessions?.length ?? 0) > 0;
  const chatScrollRef = useRailScroll<HTMLDivElement>("chats");

  const generalGroups = useMemo(
    () => groupSessions(chatList ?? []),
    [chatList],
  );

  const goNew = () => {
    if (onNewChat) {
      onNewChat();
    } else {
      // A bare /c reopens the last chat, which is not what New chat asks for.
      forgetLastChat();
      navigate({ to: "/c", search: {} });
    }
    onNavigate?.();
  };

  const handleSelect = useCallback(
    (id: string) => {
      navigate({ to: "/c", search: { id } });
      onNavigate?.();
    },
    [navigate, onNavigate],
  );

  const handleAfterRename = useCallback(() => {
    void refreshSessions();
  }, [refreshSessions]);

  const handleAfterDelete = useCallback(
    (id: string) => {
      void refreshSessions();
      if (id === activeContextId) navigate({ to: "/c", search: {} });
    },
    [refreshSessions, activeContextId, navigate],
  );

  return (
    <nav
      aria-label="Chats"
      // Fixed width, not a resizable panel. It used to be one on the chat
      // route only, so the rail jumped between 258px and 288px on every
      // navigation and the saved size made the two drift further apart.
      className="sidebar-cq flex h-full min-h-0 w-full min-w-0 flex-col border-r bg-card/30"
    >
      <div className="flex shrink-0 items-center gap-1 p-2">
        <div className="min-w-0 flex-1">
          <NewConversationMenu onGeneral={goNew} />
        </div>
        {onCollapse && (
          <Button
            variant="ghost"
            size="icon"
            className="h-9 w-9 shrink-0"
            onClick={onCollapse}
            aria-label="Hide chats"
            title="Hide chats"
          >
            <PanelLeftClose className="h-4 w-4" />
          </Button>
        )}
      </div>

      {/* Open sections split this height equally, each capped at its own
          content and floored at a header and about two rows (see
          SidebarSection). When the floors no longer fit (small windows, high
          zoom) this outer scroll takes over. */}
      <div className="flex min-h-0 flex-1 flex-col overflow-y-auto">
        <SidebarSection
          title="Chats"
          icon={MessageSquare}
          // While searching, the count follows the filter.
          count={searching ? chatList?.length : sessions?.length}
          storageKey="lha.sidebar.chats"
          defaultOpen
          headerClassName={reserveHeaderRight ? "pr-12" : undefined}
          // Header, search box and two chat rows. The empty line takes only
          // its own height.
          floorClassName="min-h-[9rem]"
          fill={chatList?.length !== 0}
          rightSlot={
            sessions && sessions.length > 0 ? (
              <DeleteAllChatsButton
                count={sessions.length}
                onAfterDelete={() => {
                  void refreshSessions();
                  // If the user is sitting on a chat that just got nuked,
                  // bounce them to a fresh chat so they don't see a phantom session.
                  if (activeContextId) navigate({ to: "/c", search: {} });
                }}
              />
            ) : undefined
          }
        >
          {showSearch && (
            <div className="shrink-0 px-2 pb-1.5">
              <SidebarSearchInput
                value={chatQuery}
                onChange={setChatQuery}
                placeholder="Search chats…"
                ariaLabel="Search chats"
              />
            </div>
          )}
          {chatLoading && !chatList ? (
            <SidebarSkeleton label="Loading chats" />
          ) : !chatList || chatList.length === 0 ? (
            <SidebarNote>
              {searching ? "No matching chats." : "No chats yet."}
            </SidebarNote>
          ) : (
            <div ref={chatScrollRef} className={cn(RAIL_SCROLL_CLASS, "pb-2")}>
              <SidebarSessionGroups
                groups={generalGroups}
                activeContextId={activeContextId}
                onSelect={handleSelect}
                onAfterRename={handleAfterRename}
                onAfterDelete={handleAfterDelete}
                onPrefetch={onPrefetch}
              />
            </div>
          )}
        </SidebarSection>

        <TopIssuesRail />
        <RecentRunsRail />
      </div>

      <div className="shrink-0 border-t px-2 py-1.5">
        <Link
          to="/config"
          className={cn(
            SIDEBAR_ROW_CLASS,
            "flex items-center gap-2 py-1.5 text-muted-foreground",
          )}
          activeProps={{ className: SIDEBAR_ROW_ACTIVE_CLASS }}
        >
          <Settings aria-hidden="true" className="h-3.5 w-3.5 shrink-0" />
          Configuration
        </Link>
      </div>
    </nav>
  );
}

export const ChatListSidebar = memo(ChatListSidebarInner);

interface DeleteAllChatsButtonProps {
  count: number;
  onAfterDelete: () => void;
}

function DeleteAllChatsButton({
  count,
  onAfterDelete,
}: DeleteAllChatsButtonProps) {
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const handleConfirm = async () => {
    setBusy(true);
    setError(null);
    try {
      const { failed } = await deleteAllLhaSessions();
      // Keep the confirmation dialog open on partial failure to display the error.
      if (failed > 0) {
        setError(
          `${failed} chat${failed === 1 ? "" : "s"} could not be deleted.`,
        );
        onAfterDelete();
        return;
      }
      setOpen(false);
      onAfterDelete();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  };

  return (
    <AlertDialog open={open} onOpenChange={setOpen}>
      <button
        type="button"
        onClick={() => setOpen(true)}
        className="inline-flex h-6 w-6 items-center justify-center rounded-md text-muted-foreground transition-colors hover:bg-destructive/15 hover:text-destructive focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
        aria-label="Delete all chats"
        title="Delete all chats"
      >
        <Trash2 className="h-3 w-3" />
      </button>
      {open && (
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Delete all chats?</AlertDialogTitle>
            <AlertDialogDescription>
              This permanently deletes {count} chat
              {count === 1 ? "" : "s"} and can&rsquo;t be undone.
            </AlertDialogDescription>
          </AlertDialogHeader>
          {error && (
            <div
              className={cn(
                textStyle.meta,
                "rounded border border-destructive/40 bg-destructive/10 px-2 py-1 text-destructive",
              )}
            >
              {error}
            </div>
          )}
          <AlertDialogFooter>
            <AlertDialogCancel disabled={busy}>Cancel</AlertDialogCancel>
            <AlertDialogAction
              disabled={busy}
              onClick={(e) => {
                e.preventDefault();
                void handleConfirm();
              }}
              className="bg-destructive text-destructive-foreground hover:bg-destructive/90"
            >
              {busy ? "Deleting…" : `Delete ${count}`}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      )}
    </AlertDialog>
  );
}
