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
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ComponentProps, ReactNode } from "react";

const { navigate } = vi.hoisted(() => ({ navigate: vi.fn() }));

vi.mock("@tanstack/react-router", () => ({
  useNavigate: () => navigate,
  Link: ({ children, to }: { children: ReactNode; to: string }) => (
    <a href={to}>{children}</a>
  ),
}));
// Each rail runs its own queries; their contents have their own tests.
vi.mock("@/components/insights/top-issues-rail", () => ({
  TopIssuesRail: () => null,
}));
vi.mock("@/components/investigations/recent-runs-rail", () => ({
  RecentRunsRail: () => null,
}));

import {
  rememberConversation,
  resetConversationsForTest,
} from "@/lib/aqua-conversations";
import type { HorizonSessionSummary } from "@/lib/horizon-sessions";
import {
  forgetLastChat,
  readLastChat,
  rememberLastChat,
} from "@/lib/last-chat";
import { NowProvider } from "@/lib/now-context";
import { ChatListSidebar, resetChatSearchForTest } from "../chat-list-sidebar";

function buildChat(id: string, title: string): HorizonSessionSummary {
  return {
    id,
    title,
    createdAt: Date.now(),
    lastUpdated: Date.now(),
    source: "user",
    jobType: null,
    workspaceWindow: [],
  };
}

function renderSidebar(
  props: Pick<
    ComponentProps<typeof ChatListSidebar>,
    "activeContextId" | "sessions"
  >,
) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: 0 } },
  });
  return render(
    <QueryClientProvider client={client}>
      <NowProvider>
        <ChatListSidebar
          sessionsLoading={false}
          refreshSessions={async () => undefined}
          {...props}
        />
      </NowProvider>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  resetConversationsForTest();
  resetChatSearchForTest();
  forgetLastChat();
  navigate.mockClear();
  vi.stubGlobal(
    "fetch",
    vi.fn(async () => Response.json({ deleted: 1 })),
  );
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("ChatListSidebar navigation", () => {
  it("opens a new chat, not the last one, from a page outside the chat", async () => {
    // PageShell's rail has no onNewChat, and a bare /c reopens the last chat.
    rememberLastChat("ctx-last");
    renderSidebar({
      activeContextId: null,
      sessions: [buildChat("ctx-last", "Earlier chat")],
    });

    await userEvent.setup().click(screen.getByTitle("New chat"));

    expect(navigate).toHaveBeenCalledWith({ to: "/c", search: {} });
    expect(readLastChat()).toBeNull();
  });

  it("leaves a chat deleted from its row while it is open", async () => {
    rememberConversation("ctx-open", "Open chat");
    rememberLastChat("ctx-open");
    renderSidebar({
      activeContextId: "ctx-open",
      sessions: [buildChat("ctx-open", "Open chat")],
    });
    const user = userEvent.setup();

    await user.click(screen.getByRole("button", { name: "Delete chat" }));
    const dialog = await screen.findByRole("alertdialog");
    await user.click(within(dialog).getByRole("button", { name: /delete/i }));

    await waitFor(() =>
      expect(navigate).toHaveBeenCalledWith({ to: "/c", search: {} }),
    );
    expect(readLastChat()).toBeNull();
  });
});
