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


import { useQuery, useQueryClient } from "@tanstack/react-query";
import { qk } from "./query-keys";
import { queryClient } from "./query-client";
import { readErrorDetail } from "./read-error-detail";

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

export function scheduledSessionsKey(enabled: boolean, q: string): string | null {
  if (!enabled) return null;
  const trimmed = q.trim();
  const base = "/lha/sessions?source=scheduler";
  return trimmed ? `${base}&q=${encodeURIComponent(trimmed)}` : base;
}

async function fetchSessions(
  source: "user" | "scheduler",
  q = "",
): Promise<HorizonSessionSummary[]> {
  const url =
    source === "user"
      ? userSessionsKey(q)
      : (scheduledSessionsKey(true, q) as string);
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

export function useLhaSessions(query = ""): {
  data: HorizonSessionSummary[] | undefined;
  error: Error | undefined;
  isLoading: boolean;
  refresh: () => Promise<unknown>;
} {
  const qc = useQueryClient();
  const q = useQuery({
    queryKey: qk.sessions("user", query),
    queryFn: () => fetchSessions("user", query),
    refetchInterval: 30_000,
    refetchOnWindowFocus: true,
    staleTime: 10_000,
  });
  return {
    data: q.data,
    error: q.error ?? undefined,
    isLoading: q.isLoading,
    refresh: () =>
      qc.invalidateQueries({ queryKey: qk.sessions("user", query) }),
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

export async function renameLhaSession(
  sessionId: string,
  title: string,
): Promise<void> {
  const url = `/lha/sessions/${encodeURIComponent(sessionId)}`;
  const r = await fetch(url, {
    method: "PATCH",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ title }),
  });
  if (!r.ok) {
    const detail = await readErrorDetail(r);
    throw new Error(`rename ${r.status}: ${detail.slice(0, 200) || "rename failed"}`);
  }
  await revalidateSessionLists();
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

export async function deleteLhaSession(sessionId: string): Promise<void> {
  const url = `/lha/sessions/${encodeURIComponent(sessionId)}`;
  const r = await fetch(url, { method: "DELETE" });
  if (!r.ok) {
    const detail = await readErrorDetail(r);
    throw new Error(`delete ${r.status}: ${detail.slice(0, 200) || "delete failed"}`);
  }
  await revalidateSessionLists();
}

export interface DeleteAllResult {
  deleted: number;
  failed: number;
}

export async function deleteAllLhaSessions(): Promise<DeleteAllResult> {
  const r = await fetch("/lha/sessions", { method: "DELETE" });
  if (!r.ok) {
    const detail = await readErrorDetail(r);
    throw new Error(
      `delete-all ${r.status}: ${detail.slice(0, 200) || "delete-all failed"}`,
    );
  }
  const body = (await r.json()) as DeleteAllResult;
  await revalidateSessionLists();
  return body;
}
