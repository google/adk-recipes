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

import { describe, it, expect, vi, beforeEach } from "vitest";
import { renderHook, waitFor } from "@testing-library/react";
import { createElement, type ReactNode } from "react";
import { QueryClientProvider, type QueryClient } from "@tanstack/react-query";
import { queryClient } from "../query-client";
import { qk } from "../query-keys";
import {
  deleteLhaSession,
  normalizeSession,
  scheduledSessionsKey,
  useLhaSessions,
  useScheduledSessions,
  userSessionsKey,
  type HorizonSessionSummary,
} from "../horizon-sessions";

function wrapper(client: QueryClient) {
  return ({ children }: { children: ReactNode }) =>
    createElement(QueryClientProvider, { client }, children);
}

const SESSIONS: HorizonSessionSummary[] = [
  {
    id: "s-1",
    title: "Road trip",
    createdAt: 1,
    lastUpdated: 2,
    source: "user",
    jobType: null,
    workspaceWindow: [],
  },
];

function fetchMock() {
  return vi.fn(async (url: string, init?: RequestInit) => {
    // Mutations (DELETE/PATCH) return ok with no body the hooks care about.
    if (init?.method === "DELETE" || init?.method === "PATCH") {
      return new Response(JSON.stringify({ deleted: 1, failed: 0 }), {
        status: 200,
      });
    }
    return new Response(JSON.stringify(SESSIONS), { status: 200 });
  });
}

beforeEach(() => {
  queryClient.clear();
  vi.stubGlobal("fetch", fetchMock());
});

describe("normalizeSession", () => {
  it("converts ADK epoch-seconds timestamps to milliseconds", () => {
    const out = normalizeSession({
      id: "s-1",
      title: "Road trip",
      createdAt: 1_700_000_000,
      lastUpdated: 1_700_000_500,
      source: "user",
      jobType: null,
    });
    expect(out.createdAt).toBe(1_700_000_000_000);
    expect(out.lastUpdated).toBe(1_700_000_500_000);
    // Missing workspaceWindow defaults to [] at the fetch boundary.
    expect(out.workspaceWindow).toEqual([]);
  });
});

describe("userSessionsKey / scheduledSessionsKey", () => {
  it("builds the bare and ?q= user list URLs", () => {
    expect(userSessionsKey("")).toBe("/lha/sessions?source=user");
    expect(userSessionsKey("  road trip ")).toBe(
      "/lha/sessions?source=user&q=road%20trip",
    );
  });

  it("returns null when scheduled is disabled, URLs otherwise", () => {
    expect(scheduledSessionsKey(false, "")).toBeNull();
    expect(scheduledSessionsKey(true, "")).toBe("/lha/sessions?source=scheduler");
    expect(scheduledSessionsKey(true, "  stand up ")).toBe(
      "/lha/sessions?source=scheduler&q=stand%20up",
    );
  });
});

describe("useLhaSessions / useScheduledSessions caching", () => {
  it("caches user sessions under qk.sessions('user', q)", async () => {
    const { result } = renderHook(() => useLhaSessions("foo"), {
      wrapper: wrapper(queryClient),
    });
    await waitFor(() => expect(result.current.isLoading).toBe(false));
    const cached = queryClient.getQueryData<HorizonSessionSummary[]>(
      qk.sessions("user", "foo"),
    );
    expect(cached).toHaveLength(1);
  });

  it("caches scheduler sessions under qk.sessions('scheduler', q)", async () => {
    const { result } = renderHook(() => useScheduledSessions(true, ""), {
      wrapper: wrapper(queryClient),
    });
    await waitFor(() => expect(result.current.isLoading).toBe(false));
    const cached = queryClient.getQueryData<HorizonSessionSummary[]>(
      qk.sessions("scheduler", ""),
    );
    expect(cached).toHaveLength(1);
  });

  it("does not fetch scheduler sessions when disabled", async () => {
    renderHook(() => useScheduledSessions(false, ""), {
      wrapper: wrapper(queryClient),
    });
    // Disabled query → no fetch.
    expect(
      (fetch as unknown as { mock: { calls: unknown[] } }).mock.calls.length,
    ).toBe(0);
  });
});

describe("prefix invalidation after a mutation", () => {
  it("refetches every session-list variant (both sources + search) on delete", async () => {
    // All three list variants share the singleton client (same one the
    // module-level mutation functions invalidate).
    renderHook(
      () => {
        useLhaSessions("");
        useLhaSessions("foo");
        useScheduledSessions(true, "");
      },
      { wrapper: wrapper(queryClient) },
    );

    // Wait for all three to populate.
    await waitFor(() => {
      expect(queryClient.getQueryData(qk.sessions("user", ""))).toBeDefined();
      expect(queryClient.getQueryData(qk.sessions("user", "foo"))).toBeDefined();
      expect(
        queryClient.getQueryData(qk.sessions("scheduler", "")),
      ).toBeDefined();
    });

    // Count GET fetches per list key before the mutation.
    const getCalls = () =>
      (fetch as unknown as { mock: { calls: [string, RequestInit?][] } }).mock
        .calls;
    const countFor = (url: string) =>
      getCalls().filter(
        ([u, init]) => u === url && (!init || init.method === undefined),
      ).length;

    const beforeUser = countFor("/lha/sessions?source=user");
    const beforeUserSearch = countFor("/lha/sessions?source=user&q=foo");
    const beforeScheduler = countFor("/lha/sessions?source=scheduler");

    await deleteLhaSession("s-1");

    // The DELETE-driven prefix invalidation must refetch ALL three variants.
    await waitFor(() => {
      expect(countFor("/lha/sessions?source=user")).toBeGreaterThan(beforeUser);
      expect(countFor("/lha/sessions?source=user&q=foo")).toBeGreaterThan(
        beforeUserSearch,
      );
      expect(countFor("/lha/sessions?source=scheduler")).toBeGreaterThan(
        beforeScheduler,
      );
    });
  });
});
