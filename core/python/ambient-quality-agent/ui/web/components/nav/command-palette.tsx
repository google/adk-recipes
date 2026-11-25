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
import { useQueries, useQuery, useQueryClient } from "@tanstack/react-query";
import { useNavigate } from "@tanstack/react-router";
import {
  House,
  Lightbulb,
  ListChecks,
  type LucideIcon,
  ScanSearch,
  Search,
  Settings,
} from "lucide-react";
import {
  insightQuery,
  insightsQuery,
  investigationQuery,
} from "@/lib/aqua-api";
import { useAgentKey } from "@/lib/agent-selection";
import { formatInvestigationStateWord } from "@/lib/investigation-summary";
import {
  type PaletteCommand,
  setCommandPaletteOpen,
  toggleCommandPalette,
  useCommandPaletteOpen,
  usePaletteCommands,
} from "@/lib/command-palette";
import {
  isIdQuery,
  listCachedInsights,
  listCachedInvestigations,
  listMatchingInsights,
  listMatchingInvestigations,
  mergeInsightMatches,
} from "@/lib/palette-search";
import { mono, textStyle } from "@/lib/typography";
import { cn, shortId } from "@/lib/utils";
import { isViewerMode } from "@/lib/viewer-mode";

/** The most insights, and the most investigations, a query lists. */
const MATCH_LIMIT = 5;

/** How long typing must pause before the server is asked, so a typed query
 *  does not send a request per keystroke. */
const LOOKUP_DELAY_MS = 250;

/** The shortest text searched on the server. One letter matches nearly every
 *  insight and says nothing about which one is wanted. */
const MIN_SEARCH_LENGTH = 2;

/** Which kind an "Open … by ID" command restricts the palette to; null while
 *  it searches everything. */
type Scope = "insight" | "investigation" | null;

interface Item {
  id: string;
  label: string;
  /** A keyboard shortcut, drawn as a key. */
  hint?: string;
  /** A short id or status, drawn as muted text. */
  detail?: string;
  Icon: LucideIcon;
  onSelect: () => void;
}

/** Returns how the investigations table words an investigation's state, so the
 *  palette does not call a stalled one "running": the record keeps the status
 *  it last wrote, and a job that died never writes another.
 *
 * @param investigation - The investigation to describe.
 * @returns The table's state word, or the stored status for an investigation
 *   that finished and has trajectories to count.
 */
function formatInvestigationDetail(
  investigation: Parameters<typeof formatInvestigationStateWord>[0],
): string {
  return (
    formatInvestigationStateWord(investigation) ?? investigation.status ?? ""
  );
}

/** Returns `value`, updated only once it has stopped changing for `delayMs`.
 *
 * @param value - The value to follow.
 * @param delayMs - How long it must stay unchanged.
 * @returns The last value that stayed unchanged for `delayMs`.
 */
function useSettledValue<T>(value: T, delayMs: number): T {
  const [settled, setSettled] = useState(value);
  useEffect(() => {
    const timer = setTimeout(() => setSettled(value), delayMs);
    return () => clearTimeout(timer);
  }, [value, delayMs]);
  return settled;
}

/** The ⌘K palette, mounted once under the root route.
 *
 * It searches the insights and investigations the dashboard has read, opens
 * either by a pasted id, and lists the commands the current page has added
 * (see `lib/command-palette`).
 */
export function CommandPalette() {
  const open = useCommandPaletteOpen();
  const pageCommands = usePaletteCommands();
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const agentKey = useAgentKey();
  const [query, setQuery] = useState("");
  const [scope, setScope] = useState<Scope>(null);
  const [activeIndex, setActiveIndex] = useState(0);
  const listRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLInputElement>(null);

  // On every route, and in a text field too: the shortcut is a request for the
  // palette wherever focus is.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") {
        e.preventDefault();
        toggleCommandPalette();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  useEffect(() => {
    if (!open) {
      setQuery("");
      setScope(null);
      setActiveIndex(0);
    }
  }, [open]);

  const q = query.trim();
  // Read when the palette opens and on each keystroke, so it searches what the
  // pages have loaded by then.
  // biome-ignore lint/correctness/useExhaustiveDependencies: open and q are what should trigger a re-read of the cache.
  const cached = useMemo(
    () => ({
      insights: listCachedInsights(queryClient, agentKey),
      investigations: listCachedInvestigations(queryClient, agentKey),
    }),
    [queryClient, agentKey, open, q],
  );
  const settledQuery = useSettledValue(q, LOOKUP_DELAY_MS);

  // Most insights are never on screen, so text is searched on the server, over
  // all of them. What the pages have loaded is listed meanwhile, so a match
  // already in hand does not wait on the request.
  const searchText =
    open &&
    scope !== "investigation" &&
    settledQuery.length >= MIN_SEARCH_LENGTH
      ? settledQuery
      : "";
  const search = useQuery({
    ...insightsQuery(agentKey, {
      query: searchText,
      pageSize: MATCH_LIMIT,
      orderBy: "impact",
    }),
    enabled: searchText !== "",
    retry: false,
  });
  const searched =
    searchText !== "" && searchText === q ? (search.data?.insights ?? []) : [];
  const insightMatches =
    scope === "investigation"
      ? []
      : mergeInsightMatches(
          listMatchingInsights(cached.insights, q, MATCH_LIMIT),
          searched,
          MATCH_LIMIT,
        );
  const investigationMatches =
    scope === "insight"
      ? []
      : listMatchingInvestigations(cached.investigations, q, MATCH_LIMIT);
  const lowerQ = q.toLowerCase();
  // A loaded item whose id starts with the query. The server reads only whole
  // ids, so once one is found there is nothing more to ask it.
  const idMatch =
    insightMatches.some((i) => i.insight_id.toLowerCase().startsWith(lowerQ)) ||
    investigationMatches.length > 0;
  const exactMatch =
    insightMatches.some((i) => i.insight_id.toLowerCase() === lowerQ) ||
    investigationMatches.some(
      (i) => i.investigation_id.toLowerCase() === lowerQ,
    );

  // An id that matches nothing loaded may still be an insight or an
  // investigation the lists did not reach. The two kinds share an id format,
  // so both detail reads are tried and whichever answers names the kind.
  const lookupId =
    open && scope === null && !idMatch && isIdQuery(settledQuery)
      ? settledQuery
      : "";
  const [insightLookup, investigationLookup] = useQueries({
    queries: [
      {
        ...insightQuery(agentKey, lookupId),
        enabled: lookupId !== "",
        retry: false,
      },
      {
        ...investigationQuery(agentKey, lookupId),
        enabled: lookupId !== "",
        retry: false,
      },
    ],
  });
  const lookupPending =
    isIdQuery(q) &&
    scope === null &&
    !idMatch &&
    (q !== settledQuery ||
      insightLookup.isLoading ||
      investigationLookup.isLoading);
  const searchPending =
    q.length >= MIN_SEARCH_LENGTH &&
    scope !== "investigation" &&
    (q !== settledQuery || search.isLoading);
  // Only for the id in the box: one still settling has not been looked up.
  const lookedUp = lookupId !== "" && lookupId === q;
  const foundInsight = lookedUp ? insightLookup.data?.insight : undefined;
  const foundInvestigation = lookedUp
    ? investigationLookup.data?.investigation
    : undefined;

  const items = useMemo<Item[]>(() => {
    const run = (action: () => void) => () => {
      setCommandPaletteOpen(false);
      action();
    };
    const openInsight = (insightId: string) =>
      run(
        () =>
          void navigate({ to: "/insights/$insightId", params: { insightId } }),
      );
    const openInvestigation = (investigationId: string) =>
      run(
        () =>
          void navigate({
            to: "/investigations/$investigationId",
            params: { investigationId },
          }),
      );
    const toItem = (c: PaletteCommand): Item => ({
      ...c,
      onSelect: run(c.onSelect),
    });

    const results: Item[] = [
      ...insightMatches.map((i) => ({
        id: `insight:${i.insight_id}`,
        label: i.refined_label || i.label || i.insight_id,
        detail: `Insight ${shortId(i.insight_id)}`,
        Icon: Lightbulb,
        onSelect: openInsight(i.insight_id),
      })),
      ...investigationMatches.map((i) => ({
        id: `investigation:${i.investigation_id}`,
        label: `Investigation ${shortId(i.investigation_id)}`,
        detail: formatInvestigationDetail(i),
        Icon: ScanSearch,
        onSelect: openInvestigation(i.investigation_id),
      })),
    ];
    if (foundInsight && !insightMatches.length) {
      results.push({
        id: `insight:${foundInsight.insight_id}`,
        label:
          foundInsight.refined_label ||
          foundInsight.label ||
          foundInsight.insight_id,
        detail: `Insight ${shortId(foundInsight.insight_id)}`,
        Icon: Lightbulb,
        onSelect: openInsight(foundInsight.insight_id),
      });
    }
    if (foundInvestigation && !investigationMatches.length) {
      results.push({
        id: `investigation:${foundInvestigation.investigation_id}`,
        label: `Investigation ${shortId(foundInvestigation.investigation_id)}`,
        detail: formatInvestigationDetail(foundInvestigation),
        Icon: ScanSearch,
        onSelect: openInvestigation(foundInvestigation.investigation_id),
      });
    }

    // The user said which kind the id is, so it can be opened without asking
    // the server first; the detail page reports an id it does not know.
    if (scope) {
      if (q && !exactMatch) {
        results.push(
          scope === "insight"
            ? {
                id: "open:insight",
                label: `Open insight ${q}`,
                Icon: Lightbulb,
                onSelect: openInsight(q),
              }
            : {
                id: "open:investigation",
                label: `Open investigation ${q}`,
                Icon: ScanSearch,
                onSelect: openInvestigation(q),
              },
        );
      }
      return results;
    }

    const choose = (next: Exclude<Scope, null>) => () => {
      setScope(next);
      setQuery("");
      setActiveIndex(0);
      // A click on the command moved focus to it; the next thing to do is
      // paste the id.
      inputRef.current?.focus();
    };
    const globalCommands: Item[] = [
      {
        id: "action:open-insight",
        label: "Open insight by ID…",
        Icon: Lightbulb,
        onSelect: choose("insight"),
      },
      {
        id: "action:open-investigation",
        label: "Open investigation by ID…",
        Icon: ScanSearch,
        onSelect: choose("investigation"),
      },
      {
        id: "nav:home",
        label: "Go to Home",
        Icon: House,
        onSelect: run(() => void navigate({ to: "/" })),
      },
      {
        id: "nav:insights",
        label: "Go to Insights",
        Icon: Lightbulb,
        onSelect: run(() => void navigate({ to: "/insights" })),
      },
      {
        id: "nav:investigations",
        label: "Go to Investigations",
        Icon: ListChecks,
        onSelect: run(() => void navigate({ to: "/investigations" })),
      },
    ];
    if (!isViewerMode()) {
      globalCommands.push({
        id: "nav:config",
        label: "Go to Configuration",
        Icon: Settings,
        onSelect: run(() => void navigate({ to: "/config" })),
      });
    }
    const commands = [
      ...pageCommands.filter((c) => c.group !== "chats").map(toItem),
      ...globalCommands,
      ...pageCommands.filter((c) => c.group === "chats").map(toItem),
    ];
    const lower = q.toLowerCase();
    return [
      ...results,
      ...(lower
        ? commands.filter((c) => c.label.toLowerCase().includes(lower))
        : commands),
    ];
  }, [
    q,
    scope,
    exactMatch,
    insightMatches,
    investigationMatches,
    foundInsight,
    foundInvestigation,
    pageCommands,
    navigate,
  ]);

  useEffect(() => {
    if (activeIndex < 0 || activeIndex >= items.length)
      setActiveIndex(Math.max(0, items.length - 1));
  }, [items.length, activeIndex]);

  const onKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === "ArrowDown") {
      e.preventDefault();
      // Never below 0: on an empty list, -1 would leave the first result that
      // arrives unselected, and Enter would do nothing.
      setActiveIndex((i) => Math.max(0, Math.min(items.length - 1, i + 1)));
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      setActiveIndex((i) => Math.max(0, i - 1));
    } else if (e.key === "Enter") {
      e.preventDefault();
      items[activeIndex]?.onSelect();
    } else if (e.key === "Backspace" && scope && query === "") {
      // Backspace past the start leaves "Open … by ID", as it would delete a
      // prefix typed before the id.
      e.preventDefault();
      setScope(null);
    }
  };

  useEffect(() => {
    listRef.current
      ?.querySelector<HTMLElement>(`[data-cmd-index="${activeIndex}"]`)
      ?.scrollIntoView({ block: "nearest" });
  }, [activeIndex]);

  const placeholder =
    scope === "insight"
      ? "Paste an insight ID…"
      : scope === "investigation"
        ? "Paste an investigation ID…"
        : "Search, or paste an insight or investigation ID…";
  const notFound =
    !lookupPending &&
    !searchPending &&
    insightMatches.length === 0 &&
    lookedUp &&
    !foundInsight &&
    !foundInvestigation &&
    insightLookup.isFetched &&
    investigationLookup.isFetched;

  return (
    <DialogPrimitive.Root open={open} onOpenChange={setCommandPaletteOpen}>
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
            Search insights, investigations and commands, or paste an id to open
            it. Arrow keys to navigate, Enter to select, Escape to close.
          </DialogPrimitive.Description>
          <div className="flex items-center gap-2 border-b px-3">
            <Search className="h-4 w-4 shrink-0 text-muted-foreground" />
            {scope && (
              <span
                className={cn(
                  textStyle.label,
                  "shrink-0 rounded bg-accent px-1.5 py-0.5",
                )}
              >
                {scope === "insight" ? "Insight" : "Investigation"}
              </span>
            )}
            <input
              ref={inputRef}
              // biome-ignore lint/a11y/noAutofocus: opening the palette is a request to type a search.
              autoFocus
              type="text"
              value={query}
              onChange={(e) => {
                setQuery(e.target.value);
                setActiveIndex(0);
              }}
              placeholder={placeholder}
              aria-label={placeholder}
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
            {items.map((it, i) => {
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
                  {it.detail && (
                    <span
                      className={cn(textStyle.meta, mono, "shrink-0 truncate")}
                    >
                      {it.detail}
                    </span>
                  )}
                  {it.hint && (
                    <kbd className={cn(textStyle.meta, mono, "shrink-0")}>
                      {it.hint}
                    </kbd>
                  )}
                </button>
              );
            })}
            {(lookupPending ||
              searchPending ||
              notFound ||
              items.length === 0) && (
              <div
                role="status"
                className={cn(textStyle.meta, "px-3 py-4 text-center")}
              >
                {lookupPending
                  ? `Looking up ${q}…`
                  : searchPending
                    ? "Searching all insights…"
                    : notFound
                      ? `No insight or investigation with ID ${q}.`
                      : "No matches."}
              </div>
            )}
          </div>
        </DialogPrimitive.Content>
      </DialogPrimitive.Portal>
    </DialogPrimitive.Root>
  );
}
