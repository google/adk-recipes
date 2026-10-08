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

import { useMemo, useSyncExternalStore } from "react";

import { queryOptions, useQuery, useQueryClient } from "@tanstack/react-query";
import { qk } from "./query-keys";
import { queryClient } from "./query-client";
import { readErrorDetail } from "./read-error-detail";
import {
  conversationsSnapshot,
  forgetConversation,
  listConversations,
  renameConversation,
  subscribeConversations,
} from "./aqua-conversations";
import { forgetLastChat } from "./last-chat";

export interface HorizonSessionSummary {
  id: string;
  title: string;
  createdAt: number;
  lastUpdated: number;
  source: "user" | "scheduler";
  jobType: "dream_review" | "routine" | null;
  // The chat's project anchor(s); empty => General. See docs projects design.
  workspaceWindow: string[];
}

// ADK session timestamps (`last_update_time`, event `timestamp`) are epoch
// SECONDS, but the UI works in milliseconds (Date.now()). Normalize at the
// fetch boundary so every consumer — the dormancy gate, relative "Xm ago"
// labels, and the Today/Yesterday grouping — compares like units.
export function normalizeSession(
  raw: Omit<HorizonSessionSummary, "workspaceWindow"> & {
    workspaceWindow?: string[];
  },
): HorizonSessionSummary {
  return {
    ...raw,
    createdAt: Math.round(raw.createdAt * 1000),
    lastUpdated: Math.round(raw.lastUpdated * 1000),
    workspaceWindow: raw.workspaceWindow ?? [],
  };
}

export function userSessionsKey(q: string): string {
  const trimmed = q.trim();
  const base = "/lha/sessions?source=user";
  return trimmed ? `${base}&q=${encodeURIComponent(trimmed)}` : base;
}

export function scheduledSessionsKey(
  enabled: boolean,
  q: string,
): string | null {
  if (!enabled) return null;
  const trimmed = q.trim();
  const base = "/lha/sessions?source=scheduler";
  return trimmed ? `${base}&q=${encodeURIComponent(trimmed)}` : base;
}

async function fetchSessions(
  source: "user" | "scheduler",
  q = "",
): Promise<HorizonSessionSummary[]> {
  // AQuA: the user's chat list lives in this browser (see
  // lib/aqua-conversations.ts for why the engine cannot produce it). The
  // scheduler list has no AQuA equivalent and stays a fetch, which answers an
  // empty list.
  if (source === "user") {
    const term = q.trim().toLowerCase();
    return listConversations()
      .filter((c) => !term || c.title.toLowerCase().includes(term))
      .map((c) => ({
        id: c.id,
        title: c.title,
        createdAt: c.createdAt,
        lastUpdated: c.lastUpdated,
        source: "user" as const,
        jobType: null,
        workspaceWindow: [],
      }));
  }
  const url = scheduledSessionsKey(true, q) as string;
  const r = await fetch(url, { cache: "no-store" });
  if (!r.ok) throw new Error(`horizon-sessions ${r.status}`);
  return ((await r.json()) as HorizonSessionSummary[]).map(normalizeSession);
}

// Invalidate every session-list view (user/scheduler, with or without ?q=)
// after a mutation. The ["lha","sessions"] array PREFIX matches every source
// and every q variant — a startsWith predicate over the query key
// predicate. Per-session reads aren't keyed under this prefix, so they're
// left untouched.
function revalidateSessionLists(): Promise<unknown> {
  return queryClient.invalidateQueries({ queryKey: ["lha", "sessions"] });
}

const NO_SESSIONS: HorizonSessionSummary[] = [];
const serverSessions = () => NO_SESSIONS;

/** One conversation the engine kept, as `/lha/contexts` reports it. */
export type StoredContext = {
  context_id: string;
  task_ids: string[];
  updated_at: string;
};

async function fetchContexts(): Promise<StoredContext[]> {
  const r = await fetch("/lha/contexts", { cache: "no-store" });
  // 501 means this deployment has no jobs bucket. That is not an empty list,
  // but for the sidebar it degrades to the same thing: show what the browser
  // knows. Throwing would blank a list the local store can still fill.
  if (r.status === 501) return [];
  if (!r.ok) throw new Error(`lha-contexts ${r.status}`);
  return ((await r.json()).contexts ?? []) as StoredContext[];
}

/**
 * The conversations the engine keeps, as `/lha/contexts` lists them. The
 * sidebar lists them and useLhaTasks replays their tasks, so both read the
 * list through this one definition of its key and query function.
 */
export function buildLhaContextsQuery() {
  return queryOptions({
    queryKey: ["lha", "contexts"],
    queryFn: fetchContexts,
    staleTime: 10_000,
  });
}

/**
 * The user's chat list, read from the browser store and `/lha/contexts`.
 *
 * Adapted from horizon, which fetches and polls `/lha/sessions`. AQuA's list
 * is local (see lib/aqua-conversations.ts), so a cached query over it was
 * only ever stale: a chat stayed missing from the sidebar for up to its
 * `staleTime` after being created, and clicking it was impossible. Reading the
 * store directly also drops a 30s poll of data that cannot change remotely.
 *
 * `data` is `undefined` until `/lha/contexts` has answered once, unless this
 * browser already has chats of its own; an empty array means neither the
 * store nor the last engine answer had any (a failed fetch counts as an empty
 * answer — the error is not surfaced).
 */
export function useLhaSessions(query = ""): {
  data: HorizonSessionSummary[] | undefined;
  error: Error | undefined;
  isLoading: boolean;
  refresh: () => Promise<unknown>;
} {
  const conversations = useSyncExternalStore(
    subscribeConversations,
    conversationsSnapshot,
    serverSessions,
  );
  // The engine is the source of truth for *which* conversations exist -- the
  // browser store only knows the ones this browser started, which is why a
  // fresh browser used to show "no chats yet" against a busy engine. Titles
  // still come from the store: the engine keeps tasks, not names.
  const server = useQuery({
    ...buildLhaContextsQuery(),
    refetchOnWindowFocus: true,
  });

  const data = useMemo(() => {
    const term = query.trim().toLowerCase();
    const local = new Map(conversations.map((c) => [c.id, c]));
    const ids = new Set<string>([
      ...conversations.map((c) => c.id),
      ...(server.data ?? []).map((c) => c.context_id),
    ]);
    // The store keeps epoch millis; `/lha/contexts` reports an ISO stamp from
    // the task's status. Parse rather than pass through, or the sort compares
    // a string to a number and orders by neither.
    const serverStamp = new Map<string, number>(
      (server.data ?? []).map((c) => [
        c.context_id,
        Date.parse(c.updated_at) || 0,
      ]),
    );
    return [...ids]
      .map((id) => {
        const c = local.get(id);
        return {
          id,
          // A conversation this browser never saw has no title. "Untitled
          // chat" is the honest label; inventing one from the id would read
          // as a name someone chose.
          title: c?.title ?? "Untitled chat",
          createdAt: c?.createdAt ?? serverStamp.get(id) ?? 0,
          lastUpdated: c?.lastUpdated ?? serverStamp.get(id) ?? 0,
          source: "user" as const,
          jobType: null,
          workspaceWindow: [],
        };
      })
      .filter((c) => !term || c.title.toLowerCase().includes(term))
      .sort((a, b) => b.lastUpdated - a.lastUpdated);
  }, [conversations, query, server.data]);

  // isPending, not isLoading: a paused (offline) or retrying query has no
  // answer yet either, and must not fall through to "No chats yet.".
  const unknown = server.isPending && conversations.length === 0;
  return {
    data: unknown ? undefined : data,
    error: undefined,
    isLoading: unknown,
    refresh: () => server.refetch(),
  };
}

export function useScheduledSessions(
  enabled: boolean,
  q: string,
): {
  data: HorizonSessionSummary[] | undefined;
  isLoading: boolean;
  refresh: () => Promise<unknown>;
} {
  const qc = useQueryClient();
  const query = useQuery({
    queryKey: qk.sessions("scheduler", q),
    queryFn: () => fetchSessions("scheduler", q),
    enabled,
    refetchInterval: 60_000,
    refetchOnWindowFocus: true,
    staleTime: 10_000,
  });
  return {
    data: query.data,
    isLoading: query.isLoading,
    refresh: () =>
      qc.invalidateQueries({ queryKey: qk.sessions("scheduler", q) }),
  };
}

/**
 * Renames a conversation stored in local storage.
 *
 * @param sessionId - Identifier of the conversation to rename.
 * @param title - New title for the conversation.
 */
export async function renameLhaSession(
  sessionId: string,
  title: string,
): Promise<void> {
  renameConversation(sessionId, title);
}

export async function setChatWindow(
  sessionId: string,
  dirs: string[],
): Promise<void> {
  const url = `/lha/sessions/${encodeURIComponent(sessionId)}/window`;
  const r = await fetch(url, {
    method: "PUT",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ dirs }),
  });
  if (!r.ok) {
    const detail = await readErrorDetail(r);
    throw new Error(
      `set-window ${r.status}: ${detail.slice(0, 200) || "set window failed"}`,
    );
  }
  await revalidateSessionLists();
}

/**
 * Deletes a conversation context and its task transcripts from the backend.
 *
 * An HTTP 501 response is treated as success for deployments without a jobs storage bucket.
 *
 * @param contextId - Identifier of the conversation context to erase.
 * @throws Error if the delete request fails.
 */
async function deleteContext(contextId: string): Promise<void> {
  const url = `/lha/contexts/${encodeURIComponent(contextId)}`;
  const r = await fetch(url, { method: "DELETE" });
  if (r.status === 501) return;
  if (!r.ok) {
    const detail = await readErrorDetail(r);
    throw new Error(
      `delete-chat ${r.status}: ${detail.slice(0, 200) || "delete failed"}`,
    );
  }
}

/**
 * Removes a deleted conversation from the cached `/lha/contexts` query.
 *
 * Pruning the cache immediately removes the conversation from the sidebar
 * without waiting for the slower background bucket scan to finish refetching,
 * preventing it from rendering as an untitled chat in the interim.
 *
 * @param contextId - Identifier of the conversation context to remove.
 */
function dropCachedContext(contextId: string): void {
  queryClient.setQueryData<StoredContext[]>(["lha", "contexts"], (cached) =>
    cached?.filter((c) => c.context_id !== contextId),
  );
}

/**
 * Deletes a conversation from both backend storage and local state.
 *
 * Deletes server-side task blobs before removing the local entry so that a failed
 * deletion preserves the conversation title rather than reverting it to an
 * untitled chat on subsequent polls.
 *
 * @param sessionId - Identifier of the conversation to delete.
 */
export async function deleteLhaSession(sessionId: string): Promise<void> {
  await deleteContext(sessionId);
  forgetConversation(sessionId);
  forgetLastChat(sessionId);
  dropCachedContext(sessionId);
  await queryClient.invalidateQueries({ queryKey: ["lha", "contexts"] });
}

export interface DeleteAllResult {
  deleted: number;
  failed: number;
}

/**
 * Deletes all conversations from backend storage and local state.
 *
 * Deletes each conversation remotely before removing its local record.
 *
 * @returns An object containing the count of successfully deleted and failed conversations.
 * @throws Error if listing backend contexts fails.
 */
export async function deleteAllLhaSessions(): Promise<DeleteAllResult> {
  const ids = new Set<string>(listConversations().map((c) => c.id));
  for (const c of await fetchContexts()) ids.add(c.context_id);

  let deleted = 0;
  let failed = 0;
  for (const id of ids) {
    try {
      await deleteContext(id);
      forgetConversation(id);
      forgetLastChat(id);
      dropCachedContext(id);
      deleted += 1;
    } catch {
      failed += 1;
    }
  }
  await queryClient.invalidateQueries({ queryKey: ["lha", "contexts"] });
  return { deleted, failed };
}
