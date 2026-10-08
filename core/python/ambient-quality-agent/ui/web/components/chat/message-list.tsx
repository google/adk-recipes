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

import { memo, useCallback, useEffect, useRef, useState } from "react";
import { ChevronDown } from "lucide-react";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";
import { MessageBubble } from "./message-bubble";
import type { ChatMessage } from "./chat-shell";

interface MessageListProps {
  messages: ChatMessage[];
  contextId: string | null;
  onResend?: (message: ChatMessage) => void;
  onEdit?: (message: ChatMessage, newText: string) => void;
  // Message id highlighted by find-in-conversation.
  highlightId?: string | null;
}

const STICK_THRESHOLD_PX = 80;

function MessageListInner({
  messages,
  contextId,
  onResend,
  onEdit,
  highlightId,
}: MessageListProps) {
  const viewportRef = useRef<HTMLDivElement>(null);
  const [stickToBottom, setStickToBottom] = useState(true);

  const handleScroll = useCallback(() => {
    const el = viewportRef.current;
    if (!el) return;
    const distanceFromBottom = el.scrollHeight - el.scrollTop - el.clientHeight;
    setStickToBottom(distanceFromBottom < STICK_THRESHOLD_PX);
  }, []);

  // biome-ignore lint/correctness/useExhaustiveDependencies(messages): scroll to bottom when messages arrive or stream.
  useEffect(() => {
    if (!stickToBottom) return;
    const el = viewportRef.current;
    if (!el) return;
    // Use "auto" to avoid queuing many smooth scrolls during streaming bursts.
    el.scrollTo({ top: el.scrollHeight, behavior: "auto" });
  }, [messages, stickToBottom]);

  const jumpToBottom = useCallback(() => {
    const el = viewportRef.current;
    if (!el) return;
    el.scrollTo({ top: el.scrollHeight, behavior: "smooth" });
    setStickToBottom(true);
  }, []);

  // End key jumps to bottom — only when not typing in a field. Helps power users
  // catch up to a long streaming run without reaching for the mouse.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== "End") return;
      const t = e.target as HTMLElement | null;
      if (
        t &&
        (t.tagName === "INPUT" ||
          t.tagName === "TEXTAREA" ||
          t.isContentEditable)
      ) {
        return;
      }
      e.preventDefault();
      jumpToBottom();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [jumpToBottom]);

  return (
    <div className="relative h-full min-h-0 flex-1">
      <div
        ref={viewportRef}
        onScroll={handleScroll}
        className="h-full overflow-y-auto [overflow-anchor:auto]"
      >
        <div className="mx-auto flex max-w-2xl flex-col gap-5 px-4 py-6">
          {messages.map((m) => (
            <div
              key={m.id}
              data-msg-id={m.id}
              className={cn(
                "scroll-mt-4 rounded-lg transition-[box-shadow,background-color] motion-safe:animate-fade-in-up",
                highlightId === m.id &&
                  "bg-primary/5 ring-2 ring-primary/50 ring-offset-2 ring-offset-background",
              )}
            >
              <MessageBubble
                message={m}
                contextId={contextId}
                onResend={onResend}
                onEdit={onEdit}
              />
            </div>
          ))}
        </div>
      </div>
      {!stickToBottom && (
        <Button
          type="button"
          variant="secondary"
          size="sm"
          onClick={jumpToBottom}
          className="absolute bottom-3 left-1/2 z-10 h-7 -translate-x-1/2 gap-1 rounded-full px-3 shadow-md motion-safe:animate-fade-in-up"
        >
          <ChevronDown className="h-3 w-3" />
          Jump to bottom
        </Button>
      )}
    </div>
  );
}

export const MessageList = memo(MessageListInner);
