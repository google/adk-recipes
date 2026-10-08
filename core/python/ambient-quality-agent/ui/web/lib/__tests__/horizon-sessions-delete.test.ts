/**
 * Copyright 2026 Google LLC
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 *
 * Unless required by applicable law or agreed to in writing, software
 * distributed under the License is distributed on an "AS IS" BASIS,
 * WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 * See the License for the specific language governing permissions and
 * limitations under the License.
 */

/**
 * Tests for deleting conversation sessions locally and on the backend.
 */
import { describe, expect, it, vi, beforeEach, afterEach } from "vitest";
import { renderHook, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import React from "react";

import {
  deleteAllLhaSessions,
  deleteLhaSession,
  useLhaSessions,
} from "../horizon-sessions";
import {
  forgetConversation,
  listConversations,
  rememberConversation,
} from "../aqua-conversations";
import { forgetLastChat, readLastChat, rememberLastChat } from "../last-chat";
import { queryClient } from "../query-client";

const CONTEXTS_KEY = ["lha", "contexts"];

/**
 * Retrieves the context IDs currently stored in the React Query cache.
 *
 * @returns Array of cached context IDs.
 */
function cachedContextIds(): string[] {
  const cached =
    queryClient.getQueryData<{ context_id: string }[]>(CONTEXTS_KEY) ?? [];
  return cached.map((c) => c.context_id);
}

/**
 * Populates the React Query cache with mock context entries for testing.
 *
 * @param ids - Context IDs to seed into the cache.
 */
function cacheContexts(...ids: string[]): void {
  queryClient.setQueryData(
    CONTEXTS_KEY,
    ids.map((id) => ({
      context_id: id,
      task_ids: ["t1"],
      updated_at: "2026-09-01T10:00:00Z",
    })),
  );
}

/**
 * Test wrapper providing an isolated React Query context.
 *
 * @param props - Component props containing child elements.
 * @returns React Query provider element wrapping the children.
 */
function wrapper({ children }: { children: React.ReactNode }) {
  const client = new QueryClient({
    defaultOptions: { queries: { gcTime: 0, retryDelay: 1 } },
  });
  return React.createElement(QueryClientProvider, { client }, children);
}

beforeEach(() => {
  // The store falls back to a module-level object when localStorage is
  // unavailable, and that survives localStorage.clear() between tests.
  for (const c of listConversations()) forgetConversation(c.id);
  // Clear the shared query client cache to prevent state from leaking across tests.
  queryClient.removeQueries({ queryKey: CONTEXTS_KEY });
  forgetLastChat();
});

afterEach(() => {
  vi.unstubAllGlobals();
});

/**
 * Mocks `fetch` to return given contexts for GET requests and a delete status for others.
 *
 * @param contexts - List of context objects returned by GET requests.
 * @param deleteStatus - HTTP status code returned for non-GET requests.
 * @returns Mock fetch function.
 */
function stubFetch(contexts: unknown[], deleteStatus = 200) {
  const fetchMock = vi.fn(async (_url: string, init?: RequestInit) => {
    if ((init?.method ?? "GET") === "GET") {
      return { ok: true, status: 200, json: async () => ({ contexts }) };
    }
    return {
      ok: deleteStatus < 400,
      status: deleteStatus,
      json: async () => ({ deleted: 1 }),
      text: async () => JSON.stringify({ detail: "bucket on fire" }),
    };
  });
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

describe("deleteLhaSession", () => {
  it("deletes the conversation server-side and drops it from the list", async () => {
    rememberConversation("ctx-a", "why did create_ticket fail?");
    const fetchMock = stubFetch([]);
    const { result } = renderHook(() => useLhaSessions(), { wrapper });
    await waitFor(() => expect(result.current.data?.length).toBe(1));

    await deleteLhaSession("ctx-a");

    expect(fetchMock).toHaveBeenCalledWith("/lha/contexts/ctx-a", {
      method: "DELETE",
    });
    await waitFor(() => expect(result.current.data?.length).toBe(0));
    expect(listConversations()).toEqual([]);
  });

  it("stops listing the chat before the refetch lands", async () => {
    // Pruning cached query data prevents the conversation from rendering as
    // "Untitled chat" while awaiting the background refetch.
    cacheContexts("ctx-a", "ctx-b");
    rememberConversation("ctx-a", "goes");
    stubFetch([]);

    await deleteLhaSession("ctx-a");

    expect(cachedContextIds()).toEqual(["ctx-b"]);
  });

  it("forgets the chat anyway when the deployment keeps no transcripts", async () => {
    // HTTP 501 indicates a deployment without a storage bucket and is treated as success.
    rememberConversation("ctx-a", "local only");
    stubFetch([], 501);

    await deleteLhaSession("ctx-a");

    expect(listConversations()).toEqual([]);
  });

  it("stops a bare /c from reopening the deleted chat", async () => {
    rememberConversation("ctx-a", "goes");
    rememberLastChat("ctx-a");
    stubFetch([]);

    await deleteLhaSession("ctx-a");

    expect(readLastChat()).toBeNull();
  });

  it("keeps the last chat when another chat is deleted", async () => {
    rememberConversation("ctx-a", "goes");
    rememberLastChat("ctx-b");
    stubFetch([]);

    await deleteLhaSession("ctx-a");

    expect(readLastChat()).toBe("ctx-b");
  });

  it("keeps the chat and its title when the engine refuses", async () => {
    rememberConversation("ctx-a", "still mine");
    stubFetch([], 500);

    await expect(deleteLhaSession("ctx-a")).rejects.toThrow(/delete-chat 500/);

    // Preserving the title prevents failed deletions from reverting to untitled chats.
    expect(listConversations().map((c) => c.title)).toEqual(["still mine"]);
  });
});

/**
 * Mocks `fetch` to simulate delete failures for specified context IDs.
 *
 * @param contexts - List of context objects returned by GET requests.
 * @param refuse - Context IDs whose DELETE requests should return HTTP 500.
 * @returns Mock fetch function.
 */
function stubFetchRefusing(contexts: unknown[], refuse: string[]) {
  const fetchMock = vi.fn(async (url: string, init?: RequestInit) => {
    if ((init?.method ?? "GET") === "GET") {
      return { ok: true, status: 200, json: async () => ({ contexts }) };
    }
    const refused = refuse.some((id) => url.endsWith(`/${id}`));
    return {
      ok: !refused,
      status: refused ? 500 : 200,
      json: async () => ({ deleted: 1 }),
      text: async () => JSON.stringify({ detail: "bucket on fire" }),
    };
  });
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

describe("deleteAllLhaSessions", () => {
  it("also deletes a conversation only the engine knows about", async () => {
    cacheContexts("ctx-local", "ctx-remote");
    rememberConversation("ctx-local", "mine");
    const fetchMock = stubFetchRefusing(
      [
        {
          context_id: "ctx-remote",
          task_ids: ["t1"],
          updated_at: "2026-09-01T10:00:00Z",
        },
      ],
      [],
    );

    await expect(deleteAllLhaSessions()).resolves.toEqual({
      deleted: 2,
      failed: 0,
    });

    for (const id of ["ctx-local", "ctx-remote"]) {
      expect(fetchMock).toHaveBeenCalledWith(`/lha/contexts/${id}`, {
        method: "DELETE",
      });
    }
    expect(listConversations()).toEqual([]);
    expect(cachedContextIds()).toEqual([]);
  });

  it("stops a bare /c from reopening any of them", async () => {
    rememberConversation("ctx-a", "goes");
    rememberLastChat("ctx-a");
    stubFetchRefusing([], []);

    await deleteAllLhaSessions();

    expect(readLastChat()).toBeNull();
  });

  it("counts the ones that failed and keeps them", async () => {
    rememberConversation("ctx-a", "goes");
    rememberConversation("ctx-b", "stays");
    stubFetchRefusing([], ["ctx-b"]);

    await expect(deleteAllLhaSessions()).resolves.toEqual({
      deleted: 1,
      failed: 1,
    });

    // Retaining failed entries prevents them from reappearing as untitled chats.
    expect(listConversations().map((c) => c.id)).toEqual(["ctx-b"]);
  });
});
