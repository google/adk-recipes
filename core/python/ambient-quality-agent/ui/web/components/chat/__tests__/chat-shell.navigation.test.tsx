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

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  act,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { QueryClientProvider } from "@tanstack/react-query";
import { TaskState, type Task } from "@a2a-js/sdk";
import {
  forwardRef,
  useImperativeHandle,
  useState,
  type ReactNode,
} from "react";

type Search = { id?: string; q?: string };

// The router, reduced to what ChatShell reads: a navigation replaces the
// search params, and the harness below passes them on as ChatRoute does.
const { router, navigate, createLhaClient } = vi.hoisted(() => {
  const router = {
    search: {} as Search,
    setSearch: (_s: Search) => {},
  };
  return {
    router,
    navigate: vi.fn((opts: { search?: Search }) =>
      router.setSearch(opts.search ?? {}),
    ),
    createLhaClient: vi.fn(),
  };
});

vi.mock("@tanstack/react-router", () => ({
  useNavigate: () => navigate,
}));
vi.mock("@/lib/a2a-client", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/a2a-client")>()),
  createLhaClient,
}));
// The rail, the panes and the palette have their own tests and their own
// queries; none of them takes part in switching conversations.
vi.mock("@/components/nav/sidebar-rail", () => ({
  SidebarRail: ({ children }: { children: ReactNode }) => <>{children}</>,
}));
vi.mock("@/components/ui/resizable", () => ({
  ResizablePanelGroup: ({ children }: { children: ReactNode }) => (
    <div>{children}</div>
  ),
  ResizablePanel: forwardRef<unknown, { children: ReactNode }>(
    ({ children }, ref) => {
      useImperativeHandle(ref, () => ({
        isCollapsed: () => true,
        expand: () => {},
        collapse: () => {},
      }));
      return <div>{children}</div>;
    },
  ),
  ResizableHandle: () => null,
}));
vi.mock("../artifact-viewer/artifact-viewer", () => ({
  ArtifactViewer: () => null,
}));
vi.mock("../command-palette", () => ({ CommandPalette: () => null }));

import type { HorizonClient } from "@/lib/a2a-client";
import {
  rememberConversation,
  rememberTask,
  resetConversationsForTest,
  taskIdsFor,
} from "@/lib/aqua-conversations";
import { readLastChat } from "@/lib/last-chat";
import { queryClient } from "@/lib/query-client";
import { _resetTaskHistoryCache } from "@/lib/task-history-cache";
import { ChatShell } from "../chat-shell";

const WELCOME = /Ask about the insights AQuA found/;

type FakeClient = HorizonClient & {
  sendStream: ReturnType<typeof vi.fn>;
};

// What getTask answers for a recorded task: one question and its answer.
const transcripts = new Map<string, [question: string, answer: string]>();
// Prompts whose turn keeps streaming until the client aborts it.
const unfinished = new Set<string>();
// While true, a boot stays pending until releaseBoots().
let holdBoots = false;
let heldBoots: Array<() => void> = [];
let clients: FakeClient[] = [];

function textPart(text: string) {
  return {
    content: { $case: "text", value: text },
    metadata: undefined,
    filename: "",
    mediaType: "",
  };
}

function recordedTask(id: string): Task {
  const [question, answer] = transcripts.get(id) ?? ["?", "?"];
  return {
    id,
    contextId: "ctx",
    status: { state: TaskState.TASK_STATE_COMPLETED },
    history: [
      { messageId: `${id}-u`, role: "user", parts: [textPart(question)] },
      { messageId: `${id}-a`, role: "agent", parts: [textPart(answer)] },
    ],
  } as unknown as Task;
}

async function* finishedTurn(taskId: string) {
  yield {
    payload: {
      $case: "statusUpdate",
      value: {
        taskId,
        status: { state: TaskState.TASK_STATE_COMPLETED },
      },
    },
  };
}

async function* unfinishedTurn(taskId: string, signal?: AbortSignal) {
  yield {
    payload: {
      $case: "statusUpdate",
      value: { taskId, status: { state: TaskState.TASK_STATE_WORKING } },
    },
  };
  // Like a network stream, it takes a moment to wind down once aborted, long
  // enough for the next chat's client to boot first.
  await new Promise((_, reject) =>
    signal?.addEventListener("abort", () =>
      setTimeout(() => reject(new DOMException("aborted", "AbortError")), 50),
    ),
  );
}

function fakeClient(contextId: string): FakeClient {
  let turns = 0;
  return {
    contextId,
    sendStream: vi.fn((text: string, opts?: { signal?: AbortSignal }) => {
      const taskId = `${contextId}-turn-${++turns}`;
      return unfinished.has(text)
        ? unfinishedTurn(taskId, opts?.signal)
        : finishedTurn(taskId);
    }),
    getTask: vi.fn(async (id: string) => recordedTask(id)),
    cancelTask: vi.fn(async () => {}),
    resubscribeTask: vi.fn(),
  } as unknown as FakeClient;
}

function releaseBoots() {
  const release = heldBoots;
  heldBoots = [];
  for (const r of release) r();
}

// The conversations each prompt was sent into, by context id.
function sentInto(prompt: string): string[] {
  return clients
    .filter((c) => c.sendStream.mock.calls.some(([text]) => text === prompt))
    .map((c) => c.contextId);
}

function Harness({ initial }: { initial: Search }) {
  const [search, setSearch] = useState(initial);
  router.search = search;
  router.setSearch = setSearch;
  return <ChatShell contextId={search.id} initialMessage={search.q} />;
}

function renderChat(initial: Search) {
  return render(
    <QueryClientProvider client={queryClient}>
      <Harness initial={initial} />
    </QueryClientProvider>,
  );
}

function status(label: string) {
  return screen.findByRole("status", { name: `agent ${label}` });
}

beforeEach(() => {
  resetConversationsForTest();
  _resetTaskHistoryCache();
  queryClient.clear();
  window.localStorage.clear();
  transcripts.clear();
  unfinished.clear();
  holdBoots = false;
  heldBoots = [];
  clients = [];
  navigate.mockClear();
  createLhaClient.mockReset();
  createLhaClient.mockImplementation(
    ({ contextId, signal }: { contextId: string; signal?: AbortSignal }) => {
      const client = fakeClient(contextId);
      clients.push(client);
      if (!holdBoots) return Promise.resolve(client);
      return new Promise((resolve, reject) => {
        signal?.addEventListener("abort", () =>
          reject(new DOMException("aborted", "AbortError")),
        );
        heldBoots.push(() => resolve(client));
      });
    },
  );
  // `/lha/contexts` and the like: a deployment without a jobs bucket.
  vi.stubGlobal(
    "fetch",
    vi.fn(async () => new Response(null, { status: 501 })),
  );
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("ChatShell switching conversations in place", () => {
  it("starts a new conversation when Chat is clicked from an open one", async () => {
    rememberConversation("cid-1", "first question");
    rememberTask("cid-1", "t-1");
    transcripts.set("t-1", ["first question", "first answer"]);
    renderChat({ id: "cid-1" });
    await screen.findByText("first answer");
    await status("Ready");

    // DiagnoseInChatButton: navigate({ to: "/c", search: { q } }).
    act(() => router.setSearch({ q: "Diagnose insight 2" }));

    await waitFor(() => expect(sentInto("Diagnose insight 2")).toHaveLength(1));
    const [started] = sentInto("Diagnose insight 2");
    expect(started).not.toBe("cid-1");
    await waitFor(() => expect(router.search).toEqual({ id: started }));
    await status("Ready");
    expect(taskIdsFor(started)).toEqual([`${started}-turn-1`]);
    expect(taskIdsFor("cid-1")).toEqual(["t-1"]);
  });

  it("starts the new conversation even while the open one is still answering", async () => {
    unfinished.add("Diagnose insight 1");
    renderChat({ q: "Diagnose insight 1" });
    await status("Thinking");
    const [first] = sentInto("Diagnose insight 1");
    expect(router.search).toEqual({ id: first });

    act(() => router.setSearch({ q: "Diagnose insight 2" }));

    await waitFor(() => expect(sentInto("Diagnose insight 2")).toHaveLength(1));
    const [second] = sentInto("Diagnose insight 2");
    expect(second).not.toBe(first);
    await waitFor(() => expect(router.search).toEqual({ id: second }));
    await status("Ready");
    expect(taskIdsFor(first)).toEqual([`${first}-turn-1`]);
    expect(taskIdsFor(second)).toEqual([`${second}-turn-1`]);
  });

  it("reconnects when the URL comes back to the conversation it just left", async () => {
    renderChat({ id: "cid-1" });
    await status("Ready");

    // A bare /c sends a cold load back to the last conversation, cid-1,
    // before the new chat's client has booted.
    holdBoots = true;
    act(() => router.setSearch({}));
    await act(async () => releaseBoots());

    await waitFor(() => expect(router.search).toEqual({ id: "cid-1" }));
    await status("Ready");
  });

  it("shows the chat being opened as loading, not as a new chat", async () => {
    rememberConversation("cid-1", "first question");
    rememberTask("cid-1", "t-1");
    transcripts.set("t-1", ["first question", "first answer"]);
    rememberConversation("cid-2", "second question");
    rememberTask("cid-2", "t-2");
    transcripts.set("t-2", ["second question", "second answer"]);
    renderChat({ id: "cid-1" });
    await screen.findByText("first answer");

    // A sidebar row: navigate({ to: "/c", search: { id } }).
    holdBoots = true;
    act(() => router.setSearch({ id: "cid-2" }));

    await status("Loading");
    expect(screen.queryByText(WELCOME)).not.toBeInTheDocument();
    await act(async () => releaseBoots());
    await screen.findByText("second answer");
    expect(screen.queryByText("first answer")).not.toBeInTheDocument();
  });

  it("remembers a new chat as the last one once something is sent in it", async () => {
    renderChat({});
    await status("Ready");
    // Nothing to return to yet: a bare /c would reopen an empty chat.
    expect(readLastChat()).toBeNull();

    act(() => router.setSearch({ q: "Diagnose insight 1" }));

    await waitFor(() => expect(sentInto("Diagnose insight 1")).toHaveLength(1));
    const [started] = sentInto("Diagnose insight 1");
    await waitFor(() => expect(readLastChat()).toBe(started));
  });

  it("opens a conversation only the server stores with its history", async () => {
    // Started in another browser, or its stream ended before it named its
    // task: either way this browser has no task ids for it.
    transcripts.set("t-r", ["remote question", "remote answer"]);
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url: string) =>
        url === "/lha/contexts"
          ? Response.json({
              contexts: [
                {
                  context_id: "cid-remote",
                  task_ids: ["t-r"],
                  updated_at: "2026-09-01T10:00:00Z",
                },
              ],
            })
          : new Response(null, { status: 501 }),
      ),
    );
    renderChat({ id: "cid-remote" });

    await screen.findByText("remote answer");
    expect(screen.queryByText(WELCOME)).not.toBeInTheDocument();
  });

  it("offers a new chat when New chat follows a chat still loading", async () => {
    rememberConversation("cid-2", "second question");
    rememberTask("cid-2", "t-2");
    holdBoots = true;
    renderChat({ id: "cid-2" });
    await status("Loading");

    // New chat, through its shortcut.
    fireEvent.keyDown(window, { key: "n", ctrlKey: true });
    await act(async () => releaseBoots());

    await status("Ready");
    expect(screen.getByText(WELCOME)).toBeInTheDocument();
  });
});
