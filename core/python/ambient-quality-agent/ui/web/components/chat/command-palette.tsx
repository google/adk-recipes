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
import * as DialogPrimitive from "@radix-ui/react-dialog";
import { useNavigate } from "@tanstack/react-router";
import {
  MessageSquarePlus,
  Pencil,
  Search,
  Folder,
  Download,
} from "lucide-react";
import { mono, textStyle } from "@/lib/typography";
import { cn } from "@/lib/utils";
import { focusComposer } from "@/lib/composer-focus";
import type { HorizonSessionSummary } from "@/lib/horizon-sessions";

interface CommandPaletteProps {
  open: boolean;
  onOpenChange: (b: boolean) => void;
  sessions: HorizonSessionSummary[] | undefined;
  activeContextId: string | null;
  onNewChat: () => void;
  onExport?: (format: "markdown" | "json") => void;
  canExport?: boolean;
}

interface Item {
  id: string;
  label: string;
  hint?: string;
  Icon: typeof Search;
  onSelect: () => void;
}

export function CommandPalette({
  open,
  onOpenChange,
  sessions,
  activeContextId,
  onNewChat,
  onExport,
  canExport = false,
}: CommandPaletteProps) {
  const navigate = useNavigate();
  const [query, setQuery] = useState("");
  const [activeIndex, setActiveIndex] = useState(0);
  const listRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) {
      setQuery("");
      setActiveIndex(0);
    }
  }, [open]);

  const items = useMemo<Item[]>(() => {
    const close = () => onOpenChange(false);
    const q = query.trim().toLowerCase();
    const actions: Item[] = [
      {
        id: "action:new-chat",
        label: "New chat",
        hint: "⌘N",
        Icon: MessageSquarePlus,
        onSelect: () => {
          onNewChat();
          close();
        },
      },
      {
        id: "action:focus-composer",
        label: "Focus message input",
        hint: "/",
        Icon: Pencil,
        onSelect: () => {
          close();
          requestAnimationFrame(focusComposer);
        },
      },
    ];
    if (canExport && onExport) {
      actions.push(
        {
          id: "action:export-markdown",
          label: "Export chat as Markdown",
          Icon: Download,
          onSelect: () => {
            onExport("markdown");
            close();
          },
        },
        {
          id: "action:export-json",
          label: "Export chat as JSON",
          Icon: Download,
          onSelect: () => {
            onExport("json");
            close();
          },
        },
      );
    }
    const sessionItems: Item[] = (sessions ?? [])
      .filter((s) => s.id !== activeContextId)
      .map((s) => ({
        id: `session:${s.id}`,
        label: s.title,
        Icon: Folder,
        onSelect: () => {
          navigate({ to: "/c", search: { id: s.id } });
          close();
        },
      }));
    const all = [...actions, ...sessionItems];
    if (!q) return all;
    return all.filter((it) => it.label.toLowerCase().includes(q));
  }, [
    query,
    sessions,
    activeContextId,
    onNewChat,
    onOpenChange,
    navigate,
    canExport,
    onExport,
  ]);

  useEffect(() => {
    if (activeIndex >= items.length)
      setActiveIndex(Math.max(0, items.length - 1));
  }, [items.length, activeIndex]);

  const onKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === "ArrowDown") {
      e.preventDefault();
      setActiveIndex((i) => Math.min(items.length - 1, i + 1));
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      setActiveIndex((i) => Math.max(0, i - 1));
    } else if (e.key === "Enter") {
      e.preventDefault();
      items[activeIndex]?.onSelect();
    }
  };

  useEffect(() => {
    listRef.current
      ?.querySelector<HTMLElement>(`[data-cmd-index="${activeIndex}"]`)
      ?.scrollIntoView({ block: "nearest" });
  }, [activeIndex]);

  return (
    <DialogPrimitive.Root open={open} onOpenChange={onOpenChange}>
      <DialogPrimitive.Portal>
        <DialogPrimitive.Overlay className="fixed inset-0 z-50 bg-black/40 backdrop-blur-sm data-[state=open]:animate-in data-[state=closed]:animate-out data-[state=open]:fade-in-0 data-[state=closed]:fade-out-0" />
        <DialogPrimitive.Content
          onKeyDown={onKeyDown}
          className="fixed left-1/2 top-[20%] z-50 w-[92vw] max-w-xl -translate-x-1/2 overflow-hidden rounded-lg border bg-background shadow-2xl data-[state=open]:animate-in data-[state=closed]:animate-out data-[state=open]:fade-in-0 data-[state=closed]:fade-out-0 data-[state=open]:zoom-in-95 data-[state=closed]:zoom-out-95"
        >
          <DialogPrimitive.Title className="sr-only">
            Command palette
          </DialogPrimitive.Title>
          <DialogPrimitive.Description className="sr-only">
            Search chats and run actions. Arrow keys to navigate, Enter to
            select, Escape to close.
          </DialogPrimitive.Description>
          <div className="flex items-center gap-2 border-b px-3">
            <Search className="h-4 w-4 shrink-0 text-muted-foreground" />
            <input
              // biome-ignore lint/a11y/noAutofocus: opening the palette is a request to type a search.
              autoFocus
              type="text"
              value={query}
              onChange={(e) => {
                setQuery(e.target.value);
                setActiveIndex(0);
              }}
              placeholder="Search chats or run an action…"
              className={cn(
                textStyle.body,
                "flex-1 bg-transparent py-3 placeholder:text-muted-foreground focus:outline-none",
              )}
            />
            <kbd className={cn(textStyle.meta, mono, "hidden sm:inline")}>
              Esc
            </kbd>
          </div>
          <div
            ref={listRef}
            className="max-h-[50vh] overflow-y-auto p-1"
            role="listbox"
            aria-label="Command palette results"
          >
            {items.length === 0 ? (
              <div className={cn(textStyle.meta, "px-3 py-6 text-center")}>
                No matches.
              </div>
            ) : (
              items.map((it, i) => {
                const Icon = it.Icon;
                const active = i === activeIndex;
                return (
                  <button
                    key={it.id}
                    type="button"
                    data-cmd-index={i}
                    role="option"
                    aria-selected={active}
                    onMouseEnter={() => setActiveIndex(i)}
                    onClick={it.onSelect}
                    className={cn(
                      textStyle.body,
                      "flex w-full items-center gap-2 rounded-md px-2 py-2 text-left transition-colors",
                      active ? "bg-accent" : "hover:bg-accent/50",
                    )}
                  >
                    <Icon className="h-4 w-4 shrink-0 text-muted-foreground" />
                    <span className="min-w-0 flex-1 truncate">{it.label}</span>
                    {it.hint && (
                      <kbd className={cn(textStyle.meta, mono, "shrink-0")}>
                        {it.hint}
                      </kbd>
                    )}
                  </button>
                );
              })
            )}
          </div>
        </DialogPrimitive.Content>
      </DialogPrimitive.Portal>
    </DialogPrimitive.Root>
  );
}
