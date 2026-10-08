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

import { describe, expect, it, vi, afterEach, beforeEach } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type React from "react";
import type { ComponentProps } from "react";

vi.mock("@tanstack/react-router", () => ({
  useNavigate: () => vi.fn(),
  Link: ({ children, to }: { children: React.ReactNode; to: string }) => (
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
import { ChatListSidebar, resetChatSearchForTest } from "../chat-list-sidebar";

beforeEach(() => {
  resetConversationsForTest();
  resetChatSearchForTest();
  window.localStorage?.clear();
  vi.stubGlobal(
    "fetch",
    vi.fn(() => new Promise(() => {})),
  );
});

afterEach(() => {
  vi.unstubAllGlobals();
});

function renderSidebar(
  props: Pick<
    ComponentProps<typeof ChatListSidebar>,
    "sessions" | "sessionsLoading"
  >,
) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: 0 } },
  });
  return render(
    <QueryClientProvider client={client}>
      <ChatListSidebar
        activeContextId={null}
        refreshSessions={async () => undefined}
        {...props}
      />
    </QueryClientProvider>,
  );
}

describe("ChatListSidebar while the chat list is unknown", () => {
  it("shows a loading skeleton, not the empty state", () => {
    renderSidebar({ sessions: undefined, sessionsLoading: true });

    expect(
      screen.getByRole("status", { name: /loading chats/i }),
    ).toBeInTheDocument();
    expect(screen.queryByText(/No chats yet/)).not.toBeInTheDocument();
  });

  it("shows the empty state only once loading finished with nothing", () => {
    renderSidebar({ sessions: [], sessionsLoading: false });

    expect(screen.getByText("No chats yet.")).toBeInTheDocument();
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
  });
});

describe("ChatListSidebar's Chats section", () => {
  function seed(): HorizonSessionSummary[] {
    rememberConversation("c1", "Why did trip bookings fail?");
    rememberConversation("c2", "RCA for refund loop");
    return ["c1", "c2"].map((id, i) => ({
      id,
      title:
        id === "c1" ? "Why did trip bookings fail?" : "RCA for refund loop",
      createdAt: i,
      lastUpdated: i,
      source: "user",
      jobType: null,
      workspaceWindow: [],
    })) as HorizonSessionSummary[];
  }

  it("has no search box and no count while there are no chats", () => {
    renderSidebar({ sessions: [], sessionsLoading: false });

    expect(
      screen.queryByRole("searchbox", { name: "Search chats" }),
    ).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Chats" })).toBeInTheDocument();
  });

  it("takes no share of the rail for its empty line", () => {
    renderSidebar({ sessions: [], sessionsLoading: false });

    expect(
      screen.getByRole("region", { name: "Chats" }).className,
    ).not.toContain("flex-1");
  });

  it("counts the chats a search leaves, not all of them", async () => {
    renderSidebar({ sessions: seed(), sessionsLoading: false });
    expect(screen.getByRole("button", { name: "Chats 2" })).toBeInTheDocument();

    await userEvent.type(
      screen.getByRole("searchbox", { name: "Search chats" }),
      "refund",
    );

    expect(screen.getByRole("button", { name: "Chats 1" })).toBeInTheDocument();
  });

  it("keeps the search across a remount, as on every route change", async () => {
    const sessions = seed();
    const first = renderSidebar({ sessions, sessionsLoading: false });
    await userEvent.type(
      screen.getByRole("searchbox", { name: "Search chats" }),
      "refund",
    );
    first.unmount();

    renderSidebar({ sessions, sessionsLoading: false });

    expect(screen.getByRole("searchbox", { name: "Search chats" })).toHaveValue(
      "refund",
    );
  });
});
