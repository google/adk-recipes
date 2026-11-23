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

/**
 * The whole app, as a read-only viewer serves it and as it is served
 * otherwise: in viewer mode no route opens chat or configuration and no
 * control that writes is drawn; without the meta tag every one of them is.
 */
import {
  RouterProvider,
  createMemoryHistory,
  createRouter,
} from "@tanstack/react-router";
import {
  cleanup,
  fireEvent,
  render,
  screen,
  within,
} from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { tourSteps } from "@/components/tour/tour-steps";
import { queryClient } from "@/lib/query-client";
import { routeTree } from "@/src/routeTree.gen";

const INSIGHT = {
  insight_id: "ins-1",
  observed_agent_key: "it_support_agent",
  label: "failed to call create_ticket",
  tool_name: "create_ticket",
  status: "NEW",
  occurrence_count: 1,
  trajectory_count: 3,
  impact: 10,
  last_investigation_at: "2026-08-28T18:35:34Z",
  diagnosis: "",
  confidence: 0,
};

const AMBIENT_HEALTH = {
  verdict: "watching",
  reason: "the last scheduled investigation ran 6 hours ago",
  schedule: "0 12 * * *",
  max_age_hours: 48,
  last_scheduled_finish: null,
  last_investigation: null,
  open_findings: 1,
  worst_finding: null,
};

/** Answers every read; returns the paths the app asked for. */
function answerEveryRead(): string[] {
  const asked: string[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL) => {
      const path = new URL(String(input), "http://x").pathname;
      asked.push(path);
      const body =
        path === "/api/insights"
          ? { insights: [INSIGHT], total: 1 }
          : path === "/api/insights/ins-1"
            ? { insight: INSIGHT, occurrences: [] }
            : path === "/api/investigations"
              ? { investigations: [] }
              : path === "/api/health"
                ? AMBIENT_HEALTH
                : {};
      return new Response(JSON.stringify(body), { status: 200 });
    }),
  );
  return asked;
}

/** Links into chat or configuration, however they are labeled. */
function countChatAndConfigLinks(): number {
  return document.querySelectorAll(
    'a[href="/c"], a[href^="/c?"], a[href^="/config"]',
  ).length;
}

function openAt(url: string) {
  return createRouter({
    routeTree,
    history: createMemoryHistory({ initialEntries: [url] }),
  });
}

function renderAppAt(url: string): void {
  // biome-ignore lint/suspicious/noExplicitAny: the test router is not the app's registered router type.
  render(<RouterProvider router={openAt(url) as any} />);
}

describe.each([
  { mode: "viewer", viewer: true },
  { mode: "default", viewer: false },
])("in $mode mode", ({ viewer }) => {
  let asked: string[] = [];

  beforeEach(() => {
    asked = answerEveryRead();
    if (viewer) {
      const meta = document.createElement("meta");
      meta.name = "aqua-viewer";
      meta.content = "1";
      document.head.prepend(meta);
    }
  });

  afterEach(() => {
    cleanup();
    // The app's own client, shared by every render: each test reads afresh.
    queryClient.clear();
    vi.unstubAllGlobals();
    document.querySelector('meta[name="aqua-viewer"]')?.remove();
  });

  it.each(["/c", "/config"])(
    "%s is reachable only outside viewer mode",
    async (path) => {
      const router = openAt(`${path}?agent=it_support_agent`);

      await router.load();

      expect(router.state.location.pathname).toBe(viewer ? "/" : path);
      expect(router.state.location.search).toMatchObject({
        agent: "it_support_agent",
      });
    },
  );

  it("shows the ambient schedule as live only outside viewer mode", async () => {
    renderAppAt("/");

    expect(
      await screen.findByRole("region", { name: "Ambient health" }),
    ).toBeInTheDocument();
    // A finished snapshot is not being watched.
    expect(screen.queryAllByText("Watching").length > 0).toBe(!viewer);
    expect(screen.queryAllByText(/daily at 12:00 UTC/).length > 0).toBe(
      !viewer,
    );
  });

  it("draws Copy as bug report on an insight only outside viewer mode", async () => {
    renderAppAt("/insights/ins-1");

    expect(
      await screen.findByRole("heading", {
        level: 1,
        name: "failed to call create_ticket",
      }),
    ).toBeInTheDocument();
    // One click would put model-written text on the OS clipboard.
    expect(
      screen.queryAllByRole("button", { name: /Copy as bug report/ }).length >
        0,
    ).toBe(!viewer);
  });

  it("draws chat, configuration and investigation controls on Home only outside viewer mode", async () => {
    renderAppAt("/");

    expect(
      await screen.findByRole("heading", { level: 1, name: "Home" }),
    ).toBeInTheDocument();
    const present = (found: unknown[]) =>
      expect(found.length > 0).toBe(!viewer);
    present(screen.queryAllByPlaceholderText(/Ask about/));
    present(screen.queryAllByRole("button", { name: "Run investigation" }));
    present(screen.queryAllByRole("link", { name: "Configuration" }));
    present(screen.queryAllByRole("button", { name: /New chat/ }));
    present(screen.queryAllByRole("region", { name: "Chats" }));
    present(screen.queryAllByRole("button", { name: "Hide chats" }));
    present(screen.queryAllByRole("navigation", { name: "Chats" }));
    // The rail stays, named for what it holds.
    const absent = (found: unknown[]) => expect(found.length > 0).toBe(viewer);
    absent(screen.queryAllByRole("button", { name: "Hide sidebar" }));
    absent(screen.queryAllByRole("navigation", { name: "Sidebar" }));
    expect(countChatAndConfigLinks() > 0).toBe(!viewer);
    await vi.waitFor(() => expect(asked).toContain("/api/stats"));
    const chatOrConfigReads = asked.filter((path) =>
      /^\/(a2a|\.well-known|lha)\b|^\/api\/(goal|memories)\b/.test(path),
    );
    if (viewer) expect(chatOrConfigReads).toEqual([]);
  });

  it("draws triage and chat controls on the insights list only outside viewer mode", async () => {
    renderAppAt("/insights");

    expect(
      await screen.findAllByText("failed to call create_ticket"),
    ).not.toHaveLength(0);
    const present = (found: unknown[]) =>
      expect(found.length > 0).toBe(!viewer);
    present(screen.queryAllByRole("button", { name: /Merge duplicates/ }));
    present(screen.queryAllByRole("button", { name: /Dismiss insight/ }));
    present(screen.queryAllByRole("button", { name: /in chat/ }));
    expect(countChatAndConfigLinks() > 0).toBe(!viewer);
  });

  it("opens the rail as a drawer on a narrow screen", async () => {
    const wide = window.matchMedia;
    // A phone-width window: every min-width query fails.
    window.matchMedia = ((query: string) => ({
      matches: false,
      media: query,
      addEventListener: () => {},
      removeEventListener: () => {},
    })) as unknown as typeof window.matchMedia;
    try {
      renderAppAt("/");
      const name = viewer ? "Show sidebar" : "Show chats";

      fireEvent.click(await screen.findByRole("button", { name }));

      const drawer = await screen.findByRole("dialog", {
        name: viewer ? "Sidebar" : "Chats",
      });
      expect(
        within(drawer).getByRole("navigation", {
          name: viewer ? "Sidebar" : "Chats",
        }),
      ).toBeInTheDocument();
      expect(
        within(drawer).queryAllByRole("button", { name: /New chat/ }).length >
          0,
      ).toBe(!viewer);
      expect(
        screen.queryAllByRole("button", {
          name: viewer ? "Show chats" : "Show sidebar",
        }),
      ).toHaveLength(0);
    } finally {
      window.matchMedia = wide;
    }
  });

  it("tours chat and triage only outside viewer mode", () => {
    const targets = tourSteps(true).map((step) => step.target);

    for (const target of ["ask", "insights-triage", "insight-actions"])
      expect(targets.includes(target)).toBe(!viewer);
    expect(targets).toContain("insight-evidence");
  });
});
