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
import { act, renderHook, waitFor } from "@testing-library/react";
import { createElement, type ReactNode } from "react";
import { QueryClientProvider, type QueryClient } from "@tanstack/react-query";
import { queryClient } from "../query-client";
import {
  listConversations,
  rememberConversation,
  resetConversationsForTest,
} from "../aqua-conversations";
import { qk } from "../query-keys";
import {
  deleteAllLhaSessions,
  deleteLhaSession,
  normalizeSession,
  renameLhaSession,
  useLhaSessions,
  useScheduledSessions,
  userSessionsKey,
  scheduledSessionsKey,
  type HorizonSessionSummary,
} from "../horizon-sessions";

function wrapper(client: QueryClient) {
  return function QueryWrapper({ children }: { children: ReactNode }) {
    return createElement(QueryClientProvider, { client }, children);
  };
}

describe("normalizeSession", () => {
  it("multiplies epoch seconds to ms and defaults workspaceWindow to empty", () => {
    const raw = {
      id: "s-1",
      title: "Title",
      createdAt: 1_700_000_000,
      lastUpdated: 1_700_000_100,
      source: "user" as const,
      jobType: null,
    };
    const s = normalizeSession(raw);
    expect(s.createdAt).toBe(1_700_000_000_000);
    expect(s.lastUpdated).toBe(1_700_000_100_000);
    expect(s.workspaceWindow).toEqual([]);
  });

  it("preserves an existing workspaceWindow", () => {
    const s = normalizeSession({
      id: "s-2",
      title: "Title",
      createdAt: 1,
      lastUpdated: 2,
      source: "user",
      jobType: null,
      workspaceWindow: ["my-proj"],
    });
    expect(s.workspaceWindow).toEqual(["my-proj"]);
  });
});

describe("URL key helpers", () => {
  it("omits ?q= when trimmed query is empty", () => {
    expect(userSessionsKey("")).toBe("/lha/sessions?source=user");
    expect(userSessionsKey("   ")).toBe("/lha/sessions?source=user");
    expect(scheduledSessionsKey(true, "")).toBe(
      "/lha/sessions?source=scheduler",
    );
  });

  it("appends url-encoded ?q= when non-empty", () => {
    expect(userSessionsKey("alpha beta")).toBe(
      "/lha/sessions?source=user&q=alpha%20beta",
    );
    expect(scheduledSessionsKey(true, "alpha/beta")).toBe(
      "/lha/sessions?source=scheduler&q=alpha%2Fbeta",
    );
  });
});

describe("useLhaSessions / useScheduledSessions caching", () => {
  beforeEach(() => {
    queryClient.clear();
    try {
      window.localStorage?.clear();
    } catch {}
    vi.stubGlobal(
      "fetch",
      vi.fn().mockImplementation(() =>
        Promise.resolve(
          new Response(
            JSON.stringify([
              {
                id: "sched-1",
                title: "Scheduled Job",
                createdAt: 1,
                lastUpdated: 2,
                source: "scheduler",
                jobType: null,
              },
            ]),
            { status: 200 },
          ),
        ),
      ),
    );
  });

  // AQuA: the user list comes from localStorage, not /lha/sessions, because
  // the engine keys one ADK user per A2A context and so cannot enumerate them.
  it("serves the user list from the store, with no query cache entry", async () => {
    // Deliberately not cached: the list is local, and a staleTime over it left
    // a brand-new chat missing from the sidebar until the cache expired.
    rememberConversation("ctx-1", "foo bar");
    const { result } = renderHook(() => useLhaSessions("foo"), {
      wrapper: wrapper(queryClient),
    });
    await waitFor(() => expect(result.current.isLoading).toBe(false));
    expect(result.current.data?.map((s) => s.id)).toEqual(["ctx-1"]);
    expect(
      queryClient.getQueryData(qk.sessions("user", "foo")),
    ).toBeUndefined();
  });

  it("shows a chat as soon as it is remembered", async () => {
    resetConversationsForTest();
    const { result } = renderHook(() => useLhaSessions(""), {
      wrapper: wrapper(queryClient),
    });
    await waitFor(() => expect(result.current.data).toHaveLength(0));
    act(() => rememberConversation("ctx-new", "just created"));
    await waitFor(() =>
      expect(result.current.data?.map((s) => s.id)).toEqual(["ctx-new"]),
    );
  });

  it("filters the user list by the search term", async () => {
    rememberConversation("ctx-keep", "insights please");
    rememberConversation("ctx-drop", "something else");
    const { result } = renderHook(() => useLhaSessions("insights"), {
      wrapper: wrapper(queryClient),
    });
    await waitFor(() => expect(result.current.isLoading).toBe(false));
    expect(result.current.data?.map((s) => s.id)).toEqual(["ctx-keep"]);
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
    expect(
      queryClient.getQueryData(qk.sessions("scheduler", "")),
    ).toBeUndefined();
  });
});

describe("prefix invalidation after a mutation", () => {
  beforeEach(() => {
    try {
      window.localStorage?.clear();
    } catch {}
    vi.stubGlobal(
      "fetch",
      vi.fn().mockImplementation((url: string, init?: RequestInit) => {
        if (init?.method === "DELETE") {
          return Promise.resolve(new Response(null, { status: 204 }));
        }
        return Promise.resolve(
          new Response(
            JSON.stringify([
              {
                id: "s-1",
                title: "Stub",
                createdAt: 1,
                lastUpdated: 2,
                source: url.includes("scheduler") ? "scheduler" : "user",
                jobType: null,
              },
            ]),
            { status: 200 },
          ),
        );
      }),
    );
  });

  it("deletes the chat's transcript as well as its local entry", async () => {
    // Deleting the server-side transcript prevents the context from reappearing
    // as an untitled chat on subsequent polls.
    resetConversationsForTest();
    rememberConversation("keep-me", "first");
    rememberConversation("delete-me", "second");

    await deleteLhaSession("delete-me");

    expect(listConversations().map((c) => c.id)).toEqual(["keep-me"]);
    expect(fetch).toHaveBeenCalledWith("/lha/contexts/delete-me", {
      method: "DELETE",
    });
  });

  it("deletes them all and reports how many", async () => {
    resetConversationsForTest();
    rememberConversation("a", "one");
    rememberConversation("b", "two");

    await expect(
      deleteAllLhaSessions({ shownKey: undefined, defaultKey: undefined }),
    ).resolves.toEqual({
      deleted: 2,
      failed: 0,
    });
    expect(listConversations()).toEqual([]);
  });
});

describe("renameLhaSession", () => {
  const CONTEXTS_KEY = ["lha", "contexts"];

  /**
   * Stubs `fetch` to return a JSON response with `status`.
   *
   * @param status - HTTP status code for the mocked response.
   * @returns The `fetch` mock spy.
   */
  function stubRename(status: number) {
    const stub = vi
      .fn()
      .mockImplementation(() =>
        Promise.resolve(
          new Response(JSON.stringify({ error: "boom" }), { status }),
        ),
      );
    vi.stubGlobal("fetch", stub);
    return stub;
  }

  beforeEach(() => {
    queryClient.clear();
    resetConversationsForTest();
    rememberConversation("r1", "original");
    queryClient.setQueryData(CONTEXTS_KEY, [
      {
        context_id: "r1",
        task_ids: ["t1"],
        updated_at: "2026-09-01T10:00:00Z",
        title: null,
        first_message: "original",
      },
    ]);
  });

  it("sends the name to the server, with the id encoded", async () => {
    const stub = stubRename(200);

    await renameLhaSession("r1/x", "renamed");

    expect(stub).toHaveBeenCalledWith("/lha/contexts/r1%2Fx/title", {
      method: "PUT",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ title: "renamed" }),
    });
  });

  it("renames locally and in the cached server list on success", async () => {
    stubRename(200);

    await renameLhaSession("r1", "renamed");

    expect(listConversations()[0].title).toBe("renamed");
    const cached =
      queryClient.getQueryData<{ context_id: string; title?: string | null }[]>(
        CONTEXTS_KEY,
      );
    expect(cached?.[0].title).toBe("renamed");
  });

  it("renames locally only when the deployment has no jobs bucket", async () => {
    stubRename(501);

    await expect(renameLhaSession("r1", "renamed")).resolves.toBeUndefined();

    expect(listConversations()[0].title).toBe("renamed");
  });

  it("throws on a server failure and keeps the old name", async () => {
    stubRename(500);

    await expect(renameLhaSession("r1", "renamed")).rejects.toThrow(
      /rename-chat 500/,
    );

    expect(listConversations()[0].title).toBe("original");
    const cached =
      queryClient.getQueryData<{ context_id: string; title?: string | null }[]>(
        CONTEXTS_KEY,
      );
    expect(cached?.[0].title).toBeNull();
  });
});
