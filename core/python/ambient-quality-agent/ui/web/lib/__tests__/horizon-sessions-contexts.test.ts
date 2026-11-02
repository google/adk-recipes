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
 * The sidebar showed CHATS (0) against a busy engine because the list was
 * whatever this browser happened to have in localStorage.
 */
import { describe, expect, it, vi, beforeEach, afterEach } from "vitest";
import { renderHook, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import React from "react";

import { useLhaSessions } from "../horizon-sessions";
import {
  forgetConversation,
  listConversations,
  rememberConversation,
  renameConversation,
} from "../aqua-conversations";

function wrapper({ children }: { children: React.ReactNode }) {
  // Retries left at the app's default on purpose: a 501 handled as an error
  // would be retried, and the retry storm is the thing worth preventing.
  // Retries stay on (the app's default) but with no backoff, so a 501 that
  // was treated as an error shows up as repeated fetches inside the test's
  // wait rather than one second later.
  const client = new QueryClient({
    defaultOptions: { queries: { gcTime: 0, retryDelay: 1 } },
  });
  return React.createElement(QueryClientProvider, { client }, children);
}

beforeEach(() => {
  // The store falls back to a module-level object when localStorage is
  // unavailable, and that survives localStorage.clear() between tests.
  for (const c of listConversations()) forgetConversation(c.id);
});

afterEach(() => {
  vi.unstubAllGlobals();
});

function stubContexts(body: unknown, status = 200) {
  vi.stubGlobal(
    "fetch",
    vi.fn(async () => ({
      ok: status < 400,
      status,
      json: async () => body,
    })),
  );
}

describe("useLhaSessions", () => {
  it("lists a conversation this browser never started", async () => {
    stubContexts({
      contexts: [
        {
          context_id: "ctx-remote",
          task_ids: ["t1"],
          updated_at: "2026-09-01T10:00:00Z",
        },
      ],
    });

    const { result } = renderHook(() => useLhaSessions(), { wrapper });

    await waitFor(() => expect(result.current.data?.length).toBe(1));
    expect(result.current.data?.[0].id).toBe("ctx-remote");
  });

  it("labels a conversation with no local title honestly", async () => {
    stubContexts({
      contexts: [
        {
          context_id: "ctx-remote",
          task_ids: [],
          updated_at: "2026-09-01T10:00:00Z",
        },
      ],
    });

    const { result } = renderHook(() => useLhaSessions(), { wrapper });

    await waitFor(() => expect(result.current.data?.length).toBe(1));
    expect(result.current.data?.[0].title).toBe("Untitled chat");
  });

  it("names a chat with the title saved on the server, over the local one", async () => {
    rememberConversation("ctx-a", "local first message");
    stubContexts({
      contexts: [
        {
          context_id: "ctx-a",
          task_ids: ["t1"],
          updated_at: "2026-09-01T10:00:00Z",
          title: "Saved name",
          first_message: "server first message",
        },
      ],
    });

    const { result } = renderHook(() => useLhaSessions(), { wrapper });

    await waitFor(() =>
      expect(result.current.data?.[0].title).toBe("Saved name"),
    );
  });

  it("prefers a name chosen in this browser over the server's first message", async () => {
    rememberConversation("ctx-a", "local first message");
    renameConversation("ctx-a", "Renamed here");
    stubContexts({
      contexts: [
        {
          context_id: "ctx-a",
          task_ids: ["t1"],
          updated_at: "2026-09-01T10:00:00Z",
          title: null,
          first_message: "server first message",
        },
      ],
    });

    const { result } = renderHook(() => useLhaSessions(), { wrapper });

    await waitFor(() => expect(result.current.data?.length).toBe(1));
    expect(result.current.data?.[0].title).toBe("Renamed here");
  });

  it("keeps the first message as the name after a reply from another browser", async () => {
    // This browser first saw the chat when it sent a reply, so its own entry
    // is titled after that reply, not after the message that opened the chat.
    rememberConversation("ctx-a", "a later reply");
    stubContexts({
      contexts: [
        {
          context_id: "ctx-a",
          task_ids: ["t1", "t2"],
          updated_at: "2026-09-01T10:00:00Z",
          title: null,
          first_message: "the opening question",
        },
      ],
    });

    const { result } = renderHook(() => useLhaSessions(), { wrapper });

    await waitFor(() =>
      expect(result.current.data?.[0].title).toBe("the opening question"),
    );
  });

  it("names a chat this browser never saw from its first message", async () => {
    // A different browser, or one whose site data was cleared, has no local
    // entry; the chat must still get a name.
    stubContexts({
      contexts: [
        {
          context_id: "ctx-remote",
          task_ids: ["t1"],
          updated_at: "2026-09-01T10:00:00Z",
          title: null,
          first_message: `why  did\n${"x".repeat(80)}`,
        },
      ],
    });

    const { result } = renderHook(() => useLhaSessions(), { wrapper });

    await waitFor(() => expect(result.current.data?.length).toBe(1));
    expect(result.current.data?.[0].title).toBe(`why did ${"x".repeat(49)}…`);
  });

  it("finds a chat by the name saved on the server", async () => {
    stubContexts({
      contexts: [
        {
          context_id: "ctx-a",
          task_ids: ["t1"],
          updated_at: "2026-09-01T10:00:00Z",
          title: "Ticket bug",
        },
        {
          context_id: "ctx-b",
          task_ids: ["t2"],
          updated_at: "2026-09-02T10:00:00Z",
          title: "Other",
        },
      ],
    });

    const { result } = renderHook(() => useLhaSessions("ticket"), {
      wrapper,
    });

    await waitFor(() => expect(result.current.data?.length).toBe(1));
    expect(result.current.data?.[0].id).toBe("ctx-a");
  });

  it("prefers the local title over the server's bare id", async () => {
    rememberConversation("ctx-a", "why did create_ticket fail?");
    stubContexts({
      contexts: [
        {
          context_id: "ctx-a",
          task_ids: ["t1"],
          updated_at: "2026-09-01T10:00:00Z",
        },
      ],
    });

    const { result } = renderHook(() => useLhaSessions(), { wrapper });

    await waitFor(() => expect(result.current.data?.length).toBe(1));
    expect(result.current.data?.[0].title).toContain("create_ticket");
  });

  it("does not list the same conversation twice", async () => {
    rememberConversation("ctx-a", "local");
    stubContexts({
      contexts: [
        {
          context_id: "ctx-a",
          task_ids: ["t1"],
          updated_at: "2026-09-01T10:00:00Z",
        },
      ],
    });

    const { result } = renderHook(() => useLhaSessions(), { wrapper });

    await waitFor(() => expect(result.current.data?.length).toBe(1));
  });

  it("still shows local chats when the endpoint is unavailable", async () => {
    rememberConversation("ctx-local", "offline chat");
    stubContexts({}, 501);

    const { result } = renderHook(() => useLhaSessions(), { wrapper });

    await waitFor(() => expect(result.current.data?.length).toBe(1));
    expect(result.current.data?.[0].id).toBe("ctx-local");
  });

  it("does not retry a deployment that has no jobs bucket", async () => {
    stubContexts({}, 501);

    renderHook(() => useLhaSessions(), { wrapper });

    // 501 is a settled fact about the deployment, not a transient failure.
    await new Promise((r) => setTimeout(r, 400));
    expect(
      (globalThis.fetch as ReturnType<typeof vi.fn>).mock.calls.length,
    ).toBe(1);
  });

  it("reports the list as unknown until /lha/contexts answers", () => {
    // A fresh browser has an empty store. Until the engine answers, that is
    // "unknown", not "no chats": rendering the empty state here is the flash.
    vi.stubGlobal(
      "fetch",
      vi.fn(() => new Promise(() => {})),
    );

    const { result } = renderHook(() => useLhaSessions(), { wrapper });

    expect(result.current.isLoading).toBe(true);
    expect(result.current.data).toBeUndefined();
  });

  it("shows this browser's chats at once while /lha/contexts is still pending", () => {
    rememberConversation("ctx-local", "already here");
    vi.stubGlobal(
      "fetch",
      vi.fn(() => new Promise(() => {})),
    );

    const { result } = renderHook(() => useLhaSessions(), { wrapper });

    expect(result.current.isLoading).toBe(false);
    expect(result.current.data?.map((c) => c.id)).toEqual(["ctx-local"]);
  });

  it("settles to an empty list once the engine reports none", async () => {
    stubContexts({ contexts: [] });

    const { result } = renderHook(() => useLhaSessions(), { wrapper });

    await waitFor(() => expect(result.current.isLoading).toBe(false));
    expect(result.current.data).toEqual([]);
  });
});
