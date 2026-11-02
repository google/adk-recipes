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
 * Tests for the chat diagnosis action on insight cards, verifying prompt
 * construction, route navigation, and interaction with merge selection mode.
 */
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import {
  createMemoryHistory,
  createRootRoute,
  createRoute,
  createRouter,
  RouterProvider,
} from "@tanstack/react-router";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { InsightView } from "@/lib/aqua-api";
import { insightRcaPrompt } from "../insight-rca-prompt";
import { InsightsView } from "../insights-view";

const INSIGHT: InsightView = {
  insight_id: "9f2c4b7e1a8d4c3fae5b60d7c1290f34",
  agent_name: "it_support_agent",
  label: "crashed with priority",
  tool_name: "create_ticket",
  status: "RECURRING",
  occurrence_count: 7,
  trace_count: 134,
  impact: 400,
  last_run_at: "2026-08-28T18:35:34Z",
  diagnosis: "Priority value must be High, Medium, Low.",
  confidence: 1.0,
};

/**
 * Renders InsightsView within a mock router and query client environment.
 *
 * @returns Function returning the current router location href.
 */
function renderPage() {
  vi.stubGlobal(
    "fetch",
    (async () =>
      new Response(JSON.stringify({ insights: [INSIGHT], total: 1 }), {
        status: 200,
      })) as typeof fetch,
  );

  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: 0 } },
  });

  const rootRoute = createRootRoute();
  const indexRoute = createRoute({
    getParentRoute: () => rootRoute,
    path: "/",
    component: () => <InsightsView />,
  });
  const detailRoute = createRoute({
    getParentRoute: () => rootRoute,
    path: "/insights/$insightId",
    component: () => <div>Detail</div>,
  });
  const chatRoute = createRoute({
    getParentRoute: () => rootRoute,
    path: "/c",
    component: () => <div>Chat</div>,
  });
  const history = createMemoryHistory({ initialEntries: ["/"] });
  const router = createRouter({
    routeTree: rootRoute.addChildren([indexRoute, detailRoute, chatRoute]),
    history,
  });

  render(
    <QueryClientProvider client={queryClient}>
      <RouterProvider router={router} />
    </QueryClientProvider>,
  );
  return () => history.location.href;
}

describe("the opening question", () => {
  it("names the insight by id and asks for a cause and a fix", () => {
    expect(insightRcaPrompt("abc123", "crashed with priority")).toBe(
      'Diagnose insight abc123 ("crashed with priority") — what is the root cause, and how would you fix it?',
    );
  });

  it("truncates a long label", () => {
    const prompt = insightRcaPrompt("abc123", "x".repeat(300));
    expect(prompt).toContain(`("${"x".repeat(119)}…")`);
  });
});

describe("the Chat action", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("opens /c with the question rather than following the card link", async () => {
    const href = renderPage();

    const chat = await screen.findByRole("button", {
      name: /Diagnose insight in chat/,
    });
    await userEvent.click(chat);

    await waitFor(() => expect(href()).toContain("/c?"));
    // Verifies the prompt uses the triage signature required for agent lookup.
    const q = new URL(href(), "http://localhost").searchParams.get("q");
    expect(q).toBe(insightRcaPrompt(INSIGHT.insight_id, INSIGHT.label));
    expect(screen.queryByText("Detail")).not.toBeInTheDocument();
  });

  it("stands aside while duplicates are being picked", async () => {
    renderPage();

    await userEvent.click(
      await screen.findByRole("button", { name: /Merge duplicates/ }),
    );

    // Row actions are hidden in merge mode so card clicks only toggle selection.
    expect(
      screen.queryByRole("button", { name: /Diagnose insight in chat/ }),
    ).not.toBeInTheDocument();
  });
});
