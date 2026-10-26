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

/** Tests sub-agent attribution across the insights list, the detail header, and
 * the bug report. Only a sub-agent is shown; the observed root agent and
 * unattributed insights show nothing. */

import { afterEach, describe, expect, it, vi } from "vitest";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import {
  createMemoryHistory,
  createRootRoute,
  createRoute,
  createRouter,
  RouterProvider,
} from "@tanstack/react-router";
import type { InsightView } from "@/lib/aqua-api";
import { InsightsView } from "../insights-view";
import { formatBugReport, InsightDetailView } from "../insight-detail";

const ROOT_AGENT = "travel_desk_agent";

function buildInsightView(over: Partial<InsightView> = {}): InsightView {
  return {
    insight_id: "ins-1",
    agent_name: ROOT_AGENT,
    label: "skipped the receipt check",
    tool_name: "submit_expense",
    status: "NEW",
    occurrence_count: 1,
    trace_count: 3,
    impact: 12,
    last_run_at: "2026-01-02T03:04:05Z",
    diagnosis: "",
    confidence: 0,
    ...over,
  };
}

/** The attribution cases: one sub-agent, and the two that render nothing. */
const SUB_AGENT = buildInsightView({ agent_id: "expense_agent" });
const ROOT = buildInsightView({ agent_id: ROOT_AGENT });
const LEGACY = buildInsightView({ agent_id: "" });

function stubFetch(body: unknown) {
  vi.stubGlobal(
    "fetch",
    (async () =>
      new Response(JSON.stringify(body), { status: 200 })) as typeof fetch,
  );
}

function renderWithRoutes(component: () => React.ReactNode) {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: 0 } },
  });
  const rootRoute = createRootRoute();
  const routes = [
    createRoute({ getParentRoute: () => rootRoute, path: "/", component }),
    createRoute({
      getParentRoute: () => rootRoute,
      path: "/insights",
      component: () => <div>All insights list</div>,
    }),
    createRoute({
      getParentRoute: () => rootRoute,
      path: "/insights/$insightId",
      component: () => <div>Detail</div>,
    }),
    createRoute({
      getParentRoute: () => rootRoute,
      path: "/c",
      component: () => <div>Chat</div>,
    }),
  ];
  const router = createRouter({
    routeTree: rootRoute.addChildren(routes),
    history: createMemoryHistory({ initialEntries: ["/"] }),
  });
  return render(
    <QueryClientProvider client={queryClient}>
      {/* biome-ignore lint/suspicious/noExplicitAny: the test router is not the app's registered router type. */}
      <RouterProvider router={router as any} />
    </QueryClientProvider>,
  );
}

afterEach(() => vi.unstubAllGlobals());

describe("the insights list row", () => {
  it("names the sub-agent a defect is attributed to", async () => {
    stubFetch({ insights: [SUB_AGENT], total: 1 });
    renderWithRoutes(() => <InsightsView />);

    const item = await screen.findByTestId("insight-agent");
    expect(item).toHaveTextContent("expense_agent");
    expect(item).toHaveAttribute(
      "title",
      "Sub-agent this defect is attributed to",
    );
  });

  it.each([
    ["the observed root agent", ROOT],
    ["an unattributed insight", LEGACY],
  ])("shows no attribution for %s", async (_, insight) => {
    stubFetch({ insights: [insight], total: 1 });
    renderWithRoutes(() => <InsightsView />);

    expect(
      await screen.findByText("skipped the receipt check"),
    ).toBeInTheDocument();
    expect(screen.queryByTestId("insight-agent")).toBeNull();
  });
});

describe("the insight detail header", () => {
  it("names the sub-agent a defect is attributed to", async () => {
    stubFetch({ insight: SUB_AGENT, occurrences: [], root_causes: [] });
    renderWithRoutes(() => <InsightDetailView insightId="ins-1" />);

    expect(await screen.findByTestId("insight-agent")).toHaveTextContent(
      "expense_agent",
    );
  });

  it.each([
    ["the observed root agent", ROOT],
    ["an unattributed insight", LEGACY],
  ])("shows no attribution for %s", async (_, insight) => {
    stubFetch({ insight, occurrences: [], root_causes: [] });
    renderWithRoutes(() => <InsightDetailView insightId="ins-1" />);

    expect(
      await screen.findByRole("heading", { name: "skipped the receipt check" }),
    ).toBeInTheDocument();
    expect(screen.queryByTestId("insight-agent")).toBeNull();
  });
});

describe("the bug report", () => {
  it("names the sub-agent right after the observed agent", () => {
    const report = formatBugReport({ insight: SUB_AGENT, occurrences: [] });

    expect(report).toContain(
      `**Observed agent:** ${ROOT_AGENT}\n**Sub-agent:** expense_agent\n`,
    );
  });

  it.each([
    ["the observed root agent", ROOT],
    ["an unattributed insight", LEGACY],
  ])("carries no sub-agent line for %s", (_, insight) => {
    const report = formatBugReport({ insight, occurrences: [] });

    expect(report).toContain(`**Observed agent:** ${ROOT_AGENT}`);
    expect(report).not.toContain("**Sub-agent:**");
  });
});
