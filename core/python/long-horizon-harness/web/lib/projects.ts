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

import { useQuery } from "@tanstack/react-query";
import { deleteLhaSession, type HorizonSessionSummary } from "./horizon-sessions";
import { qk } from "./query-keys";
import { readErrorDetail } from "./read-error-detail";

export interface ProjectGroup {
  name: string;
  chats: HorizonSessionSummary[];
}

export interface GroupedSessions {
  projects: ProjectGroup[];
  general: HorizonSessionSummary[];
}

// System / hidden workspace dirs that are never user projects.
const SYSTEM_DIRS = new Set(["lha", "uploads"]);

export function isProjectFolder(name: string): boolean {
  return Boolean(name) && !name.startsWith(".") && !SYSTEM_DIRS.has(name);
}

// A chat's project = its workspace_window anchor (first dir); empty or a
// system/hidden anchor => General. A folder shows only if it has chats OR the
// user created it (``created``) — so incidental/system folders stay hidden.
export function groupSessionsByProject(
  sessions: HorizonSessionSummary[],
  folders: string[],
  created: Set<string> = new Set(),
): GroupedSessions {
  const general: HorizonSessionSummary[] = [];
  const byName = new Map<string, HorizonSessionSummary[]>();
  for (const s of sessions) {
    const anchor = s.workspaceWindow?.[0]?.trim();
    // A chat auto-anchored to a system/hidden dir (e.g. writing lha/todos) is
    // not a real project — treat it as General.
    if (!anchor || !isProjectFolder(anchor)) {
      general.push(s);
      continue;
    }
    const list = byName.get(anchor);
    if (list) list.push(s);
    else byName.set(anchor, [s]);
  }

  const ordered: string[] = [];
  const seen = new Set<string>();
  // Folders first (tree order): only if they have chats or were user-created.
  for (const f of folders) {
    if (!isProjectFolder(f) || seen.has(f)) continue;
    const hasChats = (byName.get(f)?.length ?? 0) > 0;
    if (!hasChats && !created.has(f)) continue;
    ordered.push(f);
    seen.add(f);
  }
  // Anchor-only projects (folder not in the tree yet) always have chats.
  for (const n of [...byName.keys()].filter((n) => !seen.has(n)).sort()) {
    ordered.push(n);
    seen.add(n);
  }

  return {
    projects: ordered.map((name) => ({ name, chats: byName.get(name) ?? [] })),
    general,
  };
}

interface WorkspaceEntry {
  name: string;
  kind: "file" | "dir" | "symlink";
}

export async function fetchWorkspaceFolders(): Promise<string[]> {
  const r = await fetch("/lha/workspace/tree?path=/workspace", {
    cache: "no-store",
  });
  if (!r.ok) throw new Error(`workspace-tree ${r.status}`);
  const body = (await r.json()) as { entries?: WorkspaceEntry[] };
  return (body.entries ?? []).filter((e) => e.kind === "dir").map((e) => e.name);
}

export function useWorkspaceFolders(): {
  data: string[] | undefined;
  refresh: () => Promise<unknown>;
} {
  const q = useQuery({
    queryKey: qk.workspaceFolders(),
    queryFn: fetchWorkspaceFolders,
    refetchInterval: 60_000,
    staleTime: 10_000,
  });
  return { data: q.data, refresh: () => q.refetch() };
}

export async function createProjectFolder(name: string): Promise<{ path: string }> {
  const r = await fetch("/lha/workspace/folder", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ name }),
  });
  if (!r.ok) {
    const detail = await readErrorDetail(r);
    throw new Error(
      `create-folder ${r.status}: ${detail.slice(0, 200) || "create failed"}`,
    );
  }
  return (await r.json()) as { path: string };
}

// Hard delete: chats first, then the folder — a mid-failure leaves the folder
// (recoverable), not orphaned chats. A 404 on the folder is fine: an
// anchor-only project has no folder on disk yet.
export async function deleteProject(name: string, chatIds: string[]): Promise<void> {
  for (const id of chatIds) {
    await deleteLhaSession(id);
  }
  const path = `/workspace/${name}`;
  const r = await fetch(
    `/lha/workspace/file?path=${encodeURIComponent(path)}&recursive=true`,
    { method: "DELETE" },
  );
  if (!r.ok && r.status !== 404) {
    const detail = await readErrorDetail(r);
    throw new Error(
      `delete-folder ${r.status}: ${detail.slice(0, 200) || "delete failed"}`,
    );
  }
}
