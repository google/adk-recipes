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

import { useEffect, useMemo, useRef, useState } from "react";
import { ChevronDown, ChevronUp, Search, X } from "lucide-react";
import { textStyle } from "@/lib/typography";
import { cn } from "@/lib/utils";
import { findMatches } from "@/lib/find-in-conversation";
import type { ChatMessage } from "./chat-shell";

export function FindBar({
  messages,
  onClose,
  onActiveMatchChange,
  focusSignal,
}: {
  messages: ChatMessage[];
  onClose: () => void;
  onActiveMatchChange: (id: string | null) => void;
  // Bumped by the parent on each Cmd/Ctrl+F so a repeat press re-focuses the
  // input even when the bar is already open.
  focusSignal?: number;
}) {
  const [query, setQuery] = useState("");
  const [index, setIndex] = useState(0);
  const inputRef = useRef<HTMLInputElement>(null);

  // biome-ignore lint/correctness/useExhaustiveDependencies(focusSignal): re-focus the input on each Cmd/Ctrl+F.
  useEffect(() => {
    inputRef.current?.focus();
    inputRef.current?.select();
  }, [focusSignal]);

  const matches = useMemo(
    () => findMatches(messages, query),
    [messages, query],
  );

  // Clamp the cursor whenever the match set changes (new query, edited convo).
  useEffect(() => {
    setIndex((i) =>
      matches.length === 0 ? 0 : Math.min(i, matches.length - 1),
    );
  }, [matches]);

  // Report the active match (or null) so the parent can scroll/highlight it.
  useEffect(() => {
    onActiveMatchChange(matches[index] ?? null);
  }, [matches, index, onActiveMatchChange]);

  const step = (delta: number) => {
    if (matches.length === 0) return;
    setIndex((i) => (i + delta + matches.length) % matches.length);
  };

  const onKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === "Escape") {
      e.preventDefault();
      onClose();
    } else if (e.key === "Enter") {
      e.preventDefault();
      step(e.shiftKey ? -1 : 1);
    }
  };

  const count = matches.length === 0 ? "0/0" : `${index + 1}/${matches.length}`;

  return (
    <div className="mx-auto flex w-full max-w-2xl items-center gap-2 px-4 py-2">
      <div className="flex flex-1 items-center gap-2 rounded-md border bg-card px-2.5 py-1.5 shadow-sm">
        <Search className="h-3.5 w-3.5 shrink-0 text-muted-foreground" />
        <input
          ref={inputRef}
          // biome-ignore lint/a11y/noAutofocus: opening the find bar is a request to type a query.
          autoFocus
          type="text"
          aria-label="Find in chat"
          placeholder="Find in chat…"
          value={query}
          onChange={(e) => {
            setQuery(e.target.value);
            setIndex(0);
          }}
          onKeyDown={onKeyDown}
          className={cn(
            textStyle.body,
            "flex-1 bg-transparent placeholder:text-muted-foreground focus:outline-none",
          )}
        />
        <span
          className={cn(
            textStyle.meta,
            "shrink-0",
            matches.length === 0 && query.trim()
              ? "text-destructive"
              : "text-muted-foreground",
          )}
        >
          {count}
        </span>
      </div>
      <button
        type="button"
        aria-label="Previous match"
        onClick={() => step(-1)}
        disabled={matches.length === 0}
        className="lh-sidebar-hover rounded-md border p-1.5 text-muted-foreground disabled:opacity-40"
      >
        <ChevronUp className="h-4 w-4" />
      </button>
      <button
        type="button"
        aria-label="Next match"
        onClick={() => step(1)}
        disabled={matches.length === 0}
        className="lh-sidebar-hover rounded-md border p-1.5 text-muted-foreground disabled:opacity-40"
      >
        <ChevronDown className="h-4 w-4" />
      </button>
      <button
        type="button"
        aria-label="Close find"
        onClick={onClose}
        className="lh-sidebar-hover rounded-md border p-1.5 text-muted-foreground"
      >
        <X className="h-4 w-4" />
      </button>
    </div>
  );
}
