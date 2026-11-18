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

import { useQueryClient } from "@tanstack/react-query";
import { useNavigate, useRouter } from "@tanstack/react-router";
import {
  ChevronDown,
  MessageSquare,
  MessageSquarePlus,
  Stethoscope,
} from "lucide-react";
import { useState } from "react";
import { formatRelative } from "@/components/chat/chat-row";
import { insightRcaPrompt } from "@/components/insights/insight-rca-prompt";
import { Button, type ButtonProps } from "@/components/ui/button";
import {
  Popover,
  PopoverContent,
  PopoverTrigger,
} from "@/components/ui/popover";
import { prefetchAgentCard } from "@/lib/a2a-client";
import { useInsightChats } from "@/lib/insight-chats";
import { buildLhaContextsQuery } from "@/lib/lha-contexts";
import { useNow } from "@/lib/now-context";
import { textStyle } from "@/lib/typography";
import { cn } from "@/lib/utils";
import { isViewerMode } from "@/lib/viewer-mode";

interface DiagnoseInChatButtonProps {
  /** Unique insight ID included in the initial prompt for agent lookup. */
  insightId: string;
  /** Triage signature matched by the chat agent. */
  label: string;
  /** Accessible button label for screen readers. */
  ariaLabel: string;
  /** True when the insight already has a recorded root cause. */
  hasRootCause?: boolean;
  /** Chat context IDs from recorded root causes for this insight. */
  rootCauseContextIds?: readonly string[];
  /** Epoch-ms timestamps per root-cause context ID when full records are loaded. */
  rootCauseTimestamps?: ReadonlyMap<string, number>;
  /** Renders only the icon without text for dense layouts. */
  iconOnly?: boolean;
  variant?: ButtonProps["variant"];
  size?: ButtonProps["size"];
  className?: string;
}

/**
 * Renders an insight chat control:
 * - Undiagnosed with 0 chats: 1-click button starting a new RCA chat.
 * - Existing chat(s): 1-click button opening the latest chat paired with a
 *   caret Popover listing all sessions for the insight + "Start a new chat"
 *   (compact `iconOnly` rows open the Popover directly).
 * - Recorded root cause with 0 live chats: Popover explaining no session is on
 *   record + "Start a new chat", avoiding surprise duplicate RCA runs.
 *
 * Stops click propagation to prevent triggering parent link navigation when
 * placed inside clickable cards or rail items.
 *
 * @param props - Component properties.
 * @returns Direct chat button, split button, or Popover menu trigger, or null
 *   in a read-only viewer, which serves no chat.
 */
export function DiagnoseInChatButton({
  insightId,
  label,
  ariaLabel,
  hasRootCause = false,
  rootCauseContextIds,
  rootCauseTimestamps,
  iconOnly = false,
  variant = "ghost",
  size = "sm",
  className,
}: DiagnoseInChatButtonProps) {
  const queryClient = useQueryClient();
  const navigate = useNavigate();
  const router = useRouter();
  const now = useNow();
  const [open, setOpen] = useState(false);
  const chats = useInsightChats(
    insightId,
    rootCauseContextIds,
    rootCauseTimestamps,
  );

  if (isViewerMode()) return null;

  const warmHover = () => {
    prefetchAgentCard();
    void queryClient.prefetchQuery(buildLhaContextsQuery()).catch(() => {});
    void router.preloadRoute({ to: "/c" }).catch(() => {});
  };

  const startNewChat = () => {
    void navigate({
      to: "/c",
      search: { q: insightRcaPrompt(insightId, label), insightId },
    });
  };

  const openExistingChat = (contextId: string) => {
    void navigate({
      to: "/c",
      search: { id: contextId },
    });
  };

  if (chats.length === 0 && !hasRootCause) {
    return (
      <Button
        size={size}
        variant={variant}
        onMouseEnter={warmHover}
        onFocus={prefetchAgentCard}
        onClick={(e) => {
          e.preventDefault();
          e.stopPropagation();
          startNewChat();
        }}
        className={className}
        aria-label={ariaLabel}
      >
        <MessageSquare />
        {!iconOnly && "Chat"}
      </Button>
    );
  }

  const menuContent = (
    <PopoverContent
      align="end"
      className="w-72 p-1"
      onClick={(e) => e.stopPropagation()}
    >
      {chats.length > 0 ? (
        <>
          <p className={cn(textStyle.meta, "px-2 py-1")}>Continue in chat</p>
          <ul
            aria-label="Existing chats for this insight"
            className="flex flex-col"
          >
            {chats.map((chat) => (
              <li key={chat.id}>
                <button
                  type="button"
                  onClick={(e) => {
                    e.preventDefault();
                    e.stopPropagation();
                    setOpen(false);
                    openExistingChat(chat.id);
                  }}
                  className="flex w-full items-start gap-2 rounded-md px-2 py-1.5 text-left hover:bg-muted"
                >
                  <MessageSquare className="mt-0.5 h-3.5 w-3.5 shrink-0 text-muted-foreground" />
                  <span className="flex min-w-0 flex-1 flex-col gap-0.5">
                    <span className={cn(textStyle.body, "truncate")}>
                      {chat.title}
                    </span>
                    {(chat.lastUpdated > 0 || chat.hasRootCause) && (
                      <span
                        className={cn(
                          textStyle.meta,
                          "flex flex-wrap items-center gap-1.5",
                        )}
                      >
                        {chat.lastUpdated > 0 && (
                          <span>{formatRelative(chat.lastUpdated, now)}</span>
                        )}
                        {chat.hasRootCause && (
                          <span className="inline-flex items-center gap-1">
                            <Stethoscope className="h-3 w-3" />
                            root cause
                          </span>
                        )}
                      </span>
                    )}
                  </span>
                </button>
              </li>
            ))}
          </ul>
        </>
      ) : (
        <p className={cn(textStyle.meta, "px-2 py-1.5")}>
          No chat session on record for this root cause.
        </p>
      )}
      <div className="my-1 border-t border-border/60" />
      <button
        type="button"
        onClick={(e) => {
          e.preventDefault();
          e.stopPropagation();
          setOpen(false);
          startNewChat();
        }}
        className="flex w-full items-center gap-2 rounded-md px-2 py-1.5 text-left hover:bg-muted"
      >
        <MessageSquarePlus className="h-3.5 w-3.5 shrink-0 text-muted-foreground" />
        <span className={textStyle.body}>Start a new chat</span>
      </button>
    </PopoverContent>
  );

  if (iconOnly || chats.length === 0) {
    return (
      <Popover open={open} onOpenChange={setOpen}>
        <PopoverTrigger asChild>
          <Button
            size={size}
            variant={variant}
            onMouseEnter={warmHover}
            onFocus={prefetchAgentCard}
            onClick={(e) => {
              // Prevent parent <Link> cards from navigating while controlling the
              // Popover open state explicitly (Radix skips its own toggle on defaultPrevented).
              e.preventDefault();
              e.stopPropagation();
              setOpen((prev) => !prev);
            }}
            className={cn(
              iconOnly && "data-[state=open]:opacity-100",
              className,
            )}
            aria-label={ariaLabel}
          >
            <MessageSquare />
            {!iconOnly && (
              <>
                Chat
                <ChevronDown className="size-3 opacity-70" />
              </>
            )}
          </Button>
        </PopoverTrigger>
        {menuContent}
      </Popover>
    );
  }

  const latestChat = chats[0];
  return (
    <Popover open={open} onOpenChange={setOpen}>
      <div className="inline-flex items-center">
        <Button
          size={size}
          variant={variant}
          onMouseEnter={warmHover}
          onFocus={prefetchAgentCard}
          onClick={(e) => {
            e.preventDefault();
            e.stopPropagation();
            openExistingChat(latestChat.id);
          }}
          className={cn(className, "rounded-r-none")}
          aria-label={ariaLabel}
        >
          <MessageSquare />
          {chats.length > 1 ? `Chat (${chats.length})` : "Chat"}
        </Button>
        <PopoverTrigger asChild>
          <Button
            size={size}
            variant={variant}
            onMouseEnter={warmHover}
            onFocus={prefetchAgentCard}
            onClick={(e) => {
              e.preventDefault();
              e.stopPropagation();
              setOpen((prev) => !prev);
            }}
            className={cn(
              className,
              "rounded-l-none px-1.5",
              variant === "default"
                ? "border-l border-primary-foreground/25"
                : "border-l border-border/60",
            )}
            aria-label={`Chat options for ${insightId}`}
          >
            <ChevronDown className="size-3 opacity-80" />
          </Button>
        </PopoverTrigger>
      </div>
      {menuContent}
    </Popover>
  );
}
