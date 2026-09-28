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
import { qk } from "./query-keys";

export interface HorizonToolCall {
  name: string;
  args: string;
  ok: boolean;
  ts: number;
}

export interface HorizonInFlightToolCall {
  name: string;
  args: string;
  started_ms: number;
}

export interface HorizonDelegateRun {
  goal: string;
  status: string;
  iterations: number;
  duration_ms: number;
  summary: string;
  ts: number;
  // Present on background agent(spawn) rows; absent on blocking delegate() rows.
  task_id?: string;
}

export interface HorizonMemoryWrite {
  scope: string;
  content: string;
  ts: number;
}

export interface HorizonPendingConfirmation {
  interrupt_id: string;
  hint: string | null;
  payload: unknown;
}

export interface HorizonPendingCredential {
  interrupt_id: string;
  provider: string;
  auth_url: string | null;
  scopes: string[];
}

export interface HorizonSandboxStatus {
  status: "provisioning" | "ready" | "error";
  started_at?: string;
  ready_at?: string;
  error?: string;
  // Sandbox resource name (e.g. projects/.../sandboxes/...) — returned by
  // get_user_sandbox_state in app/sandbox/lifecycle.py.
  sandbox_name?: string;
}

export interface HorizonBoundSkill {
  name: string;
  description: string;
  source: "builtin" | "user";
}

export interface HorizonState {
  skill_telemetry: Record<string, unknown>;
  bound_skills: HorizonBoundSkill[];
  halt_reason: string | null;
  last_error: string | null;
  iteration: number;
  tool_calls_this_iteration: number;
  iteration_halted: boolean;
  iteration_halt_reason: string | null;
  session_started: boolean;
  session_started_at: string | null;
  pending_confirmation: HorizonPendingConfirmation | null;
  pending_credential: HorizonPendingCredential | null;
  tool_call_log: HorizonToolCall[];
  in_flight_tool_call: HorizonInFlightToolCall | null;
  delegate_history: HorizonDelegateRun[];
  memory_writes: HorizonMemoryWrite[];
  sandbox_status: HorizonSandboxStatus | null;
}

export interface HorizonStateResponse {
  contextId: string;
  exists: boolean;
  state: HorizonState;
}

const fetchState = async (contextId: string): Promise<HorizonStateResponse> => {
  const url = `/lha/state?context_id=${encodeURIComponent(contextId)}`;
  const r = await fetch(url, { cache: "no-store" });
  if (!r.ok) throw new Error(`lha state ${r.status}`);
  return r.json();
};

function isProvisioning(data: HorizonStateResponse | undefined): boolean {
  const status = data?.state.sandbox_status?.status;
  // Poll fast while the sandbox is still warming so the UI flips to "ready"
  // promptly; back off once we have a terminal status (or no status yet).
  return !status || status === "provisioning";
}

interface UseLhaStateOptions<T> {
  refreshMs?: number;
  // Project the data into a smaller slice. When set, TanStack Query's default
  // structural sharing preserves the slice's reference identity across polls
  // when its value is unchanged, so this hook's consumers skip re-rendering —
  // turning the 9-component-wide re-render cascade on every poll into
  // per-consumer wakes.
  select?: (data: HorizonStateResponse) => T;
}

export function useLhaState(
  contextId: string | null,
  options?: { refreshMs?: number },
): {
  data: HorizonStateResponse | undefined;
  error: Error | undefined;
  isLoading: boolean;
};
export function useLhaState<T>(
  contextId: string | null,
  options: UseLhaStateOptions<T> & { select: (data: HorizonStateResponse) => T },
): { data: T | undefined; error: Error | undefined; isLoading: boolean };
export function useLhaState<T = HorizonStateResponse>(
  contextId: string | null,
  options?: UseLhaStateOptions<T>,
): { data: T | undefined; error: Error | undefined; isLoading: boolean } {
  const refreshMs = options?.refreshMs ?? 2500;
  const select = options?.select;
  const { data, error, isLoading } = useQuery<
    HorizonStateResponse,
    Error,
    T,
    ReturnType<typeof qk.state>
  >({
    queryKey: qk.state(contextId ?? undefined),
    queryFn: () => fetchState(contextId as string),
    enabled: !!contextId,
    refetchOnWindowFocus: false,
    staleTime: 250,
    refetchInterval: (query) =>
      isProvisioning(query.state.data) ? 500 : refreshMs,
    select: select as ((data: HorizonStateResponse) => T) | undefined,
  });
  return {
    data: data as T | undefined,
    error: error ?? undefined,
    isLoading,
  };
}
