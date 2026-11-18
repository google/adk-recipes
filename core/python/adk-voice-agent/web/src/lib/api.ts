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

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useCallback } from "react";
import { latency } from "./latency";
import { byActivity } from "./projects";
import type {
  EpisodeMemories,
  EpisodeMemory,
  Me,
  MemoryField,
  OrchestratorConfig,
  OrchestratorState,
  PreferencesPatch,
  ProjectList,
  ProjectThread,
  ThreadDetail,
} from "./types";

export * from "./types";

const STATE_QUERY_KEY = ["orchestrator-state"] as const;
const CONFIG_QUERY_KEY = ["orchestrator-config"] as const;
const MEMORIES_QUERY_KEY = ["memories"] as const;
const EPISODES_QUERY_KEY = ["memory-episodes"] as const;
const PROJECTS_QUERY_KEY = ["projects"] as const;
const THREAD_QUERY_KEY = ["project-thread"] as const;
const DETAIL_QUERY_KEY = ["thread-detail"] as const;

export const API_BASE = "/api/v1";

export const authPath = (provider: string, ...parts: string[]) =>
  ["/auth", provider, ...parts].join("/");

export class ApiError extends Error {
  readonly status: number;
  readonly detail: string | undefined;

  constructor(status: number, detail?: string, fallback?: string) {
    const cleanDetail = typeof detail === "string" ? detail.trim() : "";
    const message =
      cleanDetail ||
      fallback ||
      (status === 401 || status === 403
        ? "Sign in again to continue."
        : status === 404
          ? "That couldn't be found."
          : status === 409
            ? "That request is out of date. Try again."
            : "Something went wrong. Try again.");
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.detail = cleanDetail || undefined;
  }
}

export function plainErrorMessage(
  err: unknown,
  fallback = "Something went wrong. Try again.",
): string {
  if (err instanceof ApiError) return err.message;
  if (
    err instanceof Error &&
    err.message.trim() &&
    !/failed:\s*\d{3}/i.test(err.message)
  ) {
    return err.message;
  }
  return fallback;
}

async function request<T>(
  path: string,
  method = "GET",
  body?: unknown,
): Promise<T> {
  const res = await fetch(`${API_BASE}${path}`, {
    method,
    headers:
      body === undefined ? undefined : { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    throw new ApiError(
      res.status,
      typeof data.detail === "string" ? data.detail : undefined,
    );
  }
  return data as T;
}

const postThread = (threadId: string, action: string, body?: unknown) =>
  request<ThreadDetail>(
    `/threads/${encodeURIComponent(threadId)}/${action}`,
    "POST",
    body,
  );

/** A project ID is a folder path; encode each segment separately so `/` stays a path separator. */
const threadPath = (project: string) =>
  `/projects/${project.split("/").map(encodeURIComponent).join("/")}/thread`;

export interface FocusPayload {
  view: "home" | "project" | "thread";
  project?: string;
  thread_id?: string;
}

export function postFocus(focus: FocusPayload): Promise<{ ok: boolean }> {
  return request<{ ok: boolean }>("/focus", "POST", focus);
}

export function fetchThreadDetail(threadId: string): Promise<ThreadDetail> {
  return request<ThreadDetail>(`/threads/${encodeURIComponent(threadId)}`);
}

export function approveThread(
  threadId: string,
  permissionKey?: string,
): Promise<ThreadDetail> {
  return postThread(
    threadId,
    "approve",
    permissionKey ? { permission_key: permissionKey } : undefined,
  );
}

export function messageThread(
  threadId: string,
  instruction: string,
): Promise<ThreadDetail> {
  return postThread(threadId, "message", { instruction });
}

export function cancelThread(threadId: string): Promise<ThreadDetail> {
  return postThread(threadId, "cancel");
}

export function deleteThread(
  threadId: string,
): Promise<{ ok?: boolean; deleted?: string }> {
  return request<{ ok?: boolean; deleted?: string }>(
    `/threads/${encodeURIComponent(threadId)}`,
    "DELETE",
  );
}

export function fetchProjectThread(project: string): Promise<ProjectThread> {
  return request<ProjectThread>(threadPath(project));
}

export interface ProjectUpdate {
  name?: string;
  hidden?: boolean;
}

export function updateProject(
  project: string,
  update: ProjectUpdate,
): Promise<{ id: string }> {
  return request(
    `/projects/${project.split("/").map(encodeURIComponent).join("/")}`,
    "PATCH",
    update,
  );
}

/** Deletes the project's thread history; its files stay on the computer. */
export function deleteProjectHistory(project: string): Promise<unknown> {
  return request(
    `/projects/${project.split("/").map(encodeURIComponent).join("/")}`,
    "DELETE",
  );
}

export function fetchMe() {
  return request<Me>("/me");
}

const STATE_POLL_MS = 2000;
const CARD_POLL_MS = 300;

export function useOrchestratorState() {
  return useQuery<OrchestratorState>({
    queryKey: STATE_QUERY_KEY,
    queryFn: async () => {
      const state = await request<OrchestratorState>("/state");
      latency.surfaces(state.a2ui_surfaces);
      return state;
    },
    refetchInterval: () => statePollInterval(),
  });
}

/** Poll `/state` faster while a slow tool's card is due so it appears as soon as it exists. */
export function statePollInterval(tracker = latency): number {
  return tracker.awaitingCard() ? CARD_POLL_MS : STATE_POLL_MS;
}

export function useOrchestratorConfig() {
  return useQuery<OrchestratorConfig>({
    queryKey: CONFIG_QUERY_KEY,
    queryFn: () => request<OrchestratorConfig>("/config"),
  });
}

/**
 * Pass the summary's `last_seq` as `version` so the query refetches when `/state`
 * records a new event while keeping the previous version as placeholder data.
 */
export function useThreadDetail(
  threadId: string | undefined,
  version: number,
  poll = false,
) {
  return useQuery<ThreadDetail>({
    queryKey: [...DETAIL_QUERY_KEY, threadId, poll ? "poll" : version],
    queryFn: () => fetchThreadDetail(threadId ?? ""),
    enabled: Boolean(threadId),
    staleTime: poll ? 0 : Infinity,
    refetchInterval: (query) => {
      const currentStatus = query.state.data?.status;
      if (currentStatus) return currentStatus !== "idle" ? 2000 : false;
      return poll ? 2000 : false;
    },
    placeholderData: (prev) =>
      prev?.thread_id === threadId ? prev : undefined,
  });
}

export function useProjects() {
  return useQuery({
    queryKey: PROJECTS_QUERY_KEY,
    queryFn: () => request<ProjectList>("/projects"),
    select: (data) => byActivity(data.projects),
  });
}

const threadQuery = (project: string) => ({
  queryKey: [...THREAD_QUERY_KEY, project],
  queryFn: () => fetchProjectThread(project),
});

export function useProjectThread(project: string, enabled = true) {
  return useQuery({
    ...threadQuery(project),
    enabled: enabled && Boolean(project),
  });
}

export function useRefreshProjects() {
  const qc = useQueryClient();
  return useCallback(
    () =>
      Promise.all([
        qc.invalidateQueries({ queryKey: PROJECTS_QUERY_KEY }),
        qc.invalidateQueries({ queryKey: THREAD_QUERY_KEY }),
        qc.invalidateQueries({ queryKey: DETAIL_QUERY_KEY }),
      ]),
    [qc],
  );
}

export function useRefreshThread() {
  const qc = useQueryClient();
  return useCallback(
    () =>
      Promise.all([
        qc.invalidateQueries({ queryKey: THREAD_QUERY_KEY }),
        qc.invalidateQueries({ queryKey: DETAIL_QUERY_KEY }),
      ]),
    [qc],
  );
}

export function useMemories() {
  return useQuery<{ memories: MemoryField[] }>({
    queryKey: MEMORIES_QUERY_KEY,
    queryFn: () => request<{ memories: MemoryField[] }>("/memories"),
  });
}

export function useEpisodeMemories() {
  return useQuery<EpisodeMemories>({
    queryKey: EPISODES_QUERY_KEY,
    queryFn: () => request<EpisodeMemories>("/memory-episodes"),
  });
}

export function startOAuth(provider: string) {
  return request<{ authorize_url: string; callback_origin?: string | null }>(
    authPath(provider, "authorize"),
    "POST",
  );
}

export function disconnectProvider(provider: string) {
  return request(authPath(provider, "disconnect"), "POST");
}

export function useRefreshState() {
  const qc = useQueryClient();
  return useCallback(
    () => qc.invalidateQueries({ queryKey: STATE_QUERY_KEY }),
    [qc],
  );
}

export function useRefreshConfig() {
  const qc = useQueryClient();
  return useCallback(
    () => qc.invalidateQueries({ queryKey: CONFIG_QUERY_KEY }),
    [qc],
  );
}

function withActionCopy<V, R>(
  fn: (vars: V) => Promise<R>,
  fallback: string,
): (vars: V) => Promise<R> {
  return async (vars: V) => {
    try {
      return await fn(vars);
    } catch (err) {
      if (err instanceof ApiError) {
        throw new ApiError(err.status, err.detail, fallback);
      }
      throw new ApiError(0, undefined, fallback);
    }
  };
}

/** A mutation that refreshes `/state`, the projects, and threads once it settles. */
function useStateMutation<V>(
  mutationFn: (vars: V) => Promise<unknown>,
  fallbackError: string,
  onError?: (message: string) => void,
) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: withActionCopy(mutationFn, fallbackError),
    onError: (err) => onError?.(plainErrorMessage(err, fallbackError)),
    onSettled: () =>
      Promise.all([
        qc.invalidateQueries({ queryKey: STATE_QUERY_KEY }),
        qc.invalidateQueries({ queryKey: DETAIL_QUERY_KEY }),
        qc.invalidateQueries({ queryKey: THREAD_QUERY_KEY }),
        qc.invalidateQueries({ queryKey: PROJECTS_QUERY_KEY }),
      ]),
  });
}

/** A mutation that refreshes `/config` once it settles, whether it succeeded or not. */
function useConfigMutation<V>(
  mutationFn: (vars: V) => Promise<unknown>,
  fallbackError: string,
  onError?: (message: string) => void,
) {
  const refresh = useRefreshConfig();
  return useMutation({
    mutationFn: withActionCopy(mutationFn, fallbackError),
    onError: (err) => onError?.(plainErrorMessage(err, fallbackError)),
    onSettled: refresh,
  });
}

/** A mutation that refreshes `/memories` once it settles, whether it succeeded or not. */
function useMemoriesMutation<V>(
  mutationFn: (vars: V) => Promise<unknown>,
  fallbackError: string,
  onError?: (message: string) => void,
) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: withActionCopy(mutationFn, fallbackError),
    onError: (err) => onError?.(plainErrorMessage(err, fallbackError)),
    onSettled: () => {
      qc.invalidateQueries({ queryKey: MEMORIES_QUERY_KEY });
      qc.invalidateQueries({ queryKey: EPISODES_QUERY_KEY });
    },
  });
}

export function useOrchestratorActions(onError?: (message: string) => void) {
  return {
    setHarness: useConfigMutation(
      (harness: string) => request("/harnesses/default", "POST", { harness }),
      "Couldn't switch coding agent. Try again.",
      onError,
    ),
    messageThread: useStateMutation(
      ({ threadId, instruction }: { threadId: string; instruction: string }) =>
        messageThread(threadId, instruction),
      "Couldn't update this thread. Try again.",
      onError,
    ),
    approveThread: useStateMutation(
      ({
        threadId,
        permissionKey,
      }: {
        threadId: string;
        permissionKey?: string;
      }) => approveThread(threadId, permissionKey),
      "Couldn't approve. Try again.",
      onError,
    ),
    cancelThread: useStateMutation(
      (threadId: string) => cancelThread(threadId),
      "Couldn't stop this thread. Try again.",
      onError,
    ),
    deleteThread: useStateMutation(
      (threadId: string) => deleteThread(threadId),
      "Couldn't delete this thread. Try again.",
      onError,
    ),
    /** Deletes projects' threads and history (their files stay); one or many. */
    deleteProjects: useStateMutation(
      (projects: string[]) =>
        Promise.all(projects.map((project) => deleteProjectHistory(project))),
      "Couldn't clear project history. Try again.",
      onError,
    ),
    /** Renames a project or hides it; the folder on the computer keeps its name. */
    updateProject: useStateMutation(
      ({ project, update }: { project: string; update: ProjectUpdate }) =>
        updateProject(project, update),
      "Couldn't update project. Try again.",
      onError,
    ),
    dismissSurface: useStateMutation(
      (surfaceId: string) =>
        request("/a2ui/dismiss", "POST", { surface_id: surfaceId }),
      "Couldn't dismiss card. Try again.",
      onError,
    ),
    disconnect: useConfigMutation(
      (provider: string) => disconnectProvider(provider),
      "Couldn't disconnect app. Try again.",
      onError,
    ),
    updatePreferences: useConfigMutation(
      (patch: PreferencesPatch) => request("/preferences", "PATCH", patch),
      "Couldn't save your preferences. Try again.",
      onError,
    ),
    forgetMemory: useMemoriesMutation(
      (memoryId: string) =>
        request(`/memories/${encodeURIComponent(memoryId)}`, "DELETE"),
      "Couldn't forget this memory. Try again.",
      onError,
    ),
    forgetEpisode: useMemoriesMutation(
      ({ kind, id }: { kind: EpisodeMemory["kind"]; id: string }) =>
        request(`/memory-episodes/${kind}/${encodeURIComponent(id)}`, "DELETE"),
      "Couldn't forget this memory. Try again.",
      onError,
    ),
  };
}
