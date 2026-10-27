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
 * The engine caps a detail read at 30 occurrences and says so with a
 * `next_page_token`. The pane reports what it holds against what was recorded.
 */
import { describe, expect, it, vi, beforeEach } from "vitest";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import {
  createMemoryHistory,
  createRootRoute,
  createRoute,
  createRouter,
  RouterProvider,
} from "@tanstack/react-router";
import { InsightDetailView } from "../insight-detail";

const occurrence = (n: number) => ({
  occurrence_id: `occ-${n}`,
  run_id: `run-000000${String(n).padStart(2, "0")}`,
  created_at: `2026-09-14T10:00:00Z`,
  confidence: 0,
  diagnosis: "",
  label: `sighting ${n}`,
  item_count: 1,
  trace_count: 1,
  cases_checked: 0,
  evidence_case_ids: [],
  console_urls: {},
  rubrics: [],
});

/** Serves `shown` occurrences of an insight that records `recorded` of them. */
function renderDetail(
  shown: number,
  recorded: number,
  nextPageToken: string | null,
) {
  vi.stubGlobal("fetch", (() =>
    Promise.resolve(
      new Response(
        JSON.stringify({
          insight: {
            insight_id: "ins-1",
            agent_name: "it_support_agent",
            label: "calls the tool with a bad category",
            tool_name: "create_ticket",
            status: "RECURRING",
            occurrence_count: recorded,
            trace_count: 9,
            impact: 9,
            last_run_at: "2026-09-14T10:00:00Z",
            diagnosis: "",
            confidence: 0,
          },
          occurrences: Array.from({ length: shown }, (_, i) =>
            occurrence(i + 1),
          ),
          next_page_token: nextPageToken,
        }),
        { status: 200 },
      ),
    )) as typeof fetch);
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: 0 } },
  });
  const rootRoute = createRootRoute();
  const routes = [
    createRoute({
      getParentRoute: () => rootRoute,
      path: "/",
      component: () => <InsightDetailView insightId="ins-1" />,
    }),
    createRoute({
      getParentRoute: () => rootRoute,
      path: "/investigations/$runId",
      component: () => <div>Run</div>,
    }),
    createRoute({
      getParentRoute: () => rootRoute,
      path: "/insights",
      component: () => <div>List</div>,
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

describe("an insight's occurrence history when the engine capped the read", () => {
  beforeEach(() => vi.restoreAllMocks());

  it("counts what it shows against what was recorded", async () => {
    renderDetail(30, 86, "b2Zmc2V0OjMw");

    expect(await screen.findByText("History (30 of 86)")).toBeInTheDocument();
  });

  it("says how many sightings it is not showing", async () => {
    renderDetail(30, 86, "b2Zmc2V0OjMw");

    expect(
      await screen.findByText(/56 earlier occurrences are not shown/),
    ).toBeInTheDocument();
  });

  it("stays quiet when the page is the whole history", async () => {
    renderDetail(4, 4, null);

    expect(await screen.findByText("History (4 of 4)")).toBeInTheDocument();
    expect(screen.queryByText(/not shown/)).toBeNull();
  });

  it("reads singular for a single withheld sighting", async () => {
    renderDetail(30, 31, "b2Zmc2V0OjMw");

    expect(
      await screen.findByText(/1 earlier occurrence is not shown/),
    ).toBeInTheDocument();
  });

  it("still admits a shortfall when the recorded count is behind the page", async () => {
    // The token is the engine's own word that it held more back. A stale or
    // absent `occurrence_count` must not turn that into silence.
    renderDetail(30, 0, "b2Zmc2V0OjMw");

    expect(await screen.findByText("History (30 of 30)")).toBeInTheDocument();
    expect(
      await screen.findByText(/1 earlier occurrence is not shown/),
    ).toBeInTheDocument();
  });
});
