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

import { describe, expect, it, vi, beforeEach } from "vitest";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import {
  createMemoryHistory,
  createRootRoute,
  createRoute,
  createRouter,
  RouterProvider,
} from "@tanstack/react-router";
import type { InsightView } from "@/lib/aqua-api";
import { InsightsView } from "../insights-view";

const INSIGHT_HIGH: InsightView = {
  insight_id: "ins-high",
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

const INSIGHT_LOW: InsightView = {
  insight_id: "ins-low",
  agent_name: "it_support_agent",
  label: "failed to call create_ticket",
  tool_name: "create_ticket",
  status: "NEW",
  occurrence_count: 1,
  trace_count: 3,
  impact: 10,
  last_run_at: "2026-08-28T18:35:34Z",
  diagnosis: "",
  confidence: 0,
};

function renderPage(
  fetchImpl: typeof fetch,
  props: { runId?: string; embedded?: boolean } = {},
) {
  vi.stubGlobal("fetch", fetchImpl);
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: 0 } },
  });

  const rootRoute = createRootRoute();
  const indexRoute = createRoute({
    getParentRoute: () => rootRoute,
    path: "/",
    component: () => <InsightsView {...props} />,
  });
  const detailRoute = createRoute({
    getParentRoute: () => rootRoute,
    path: "/insights/$insightId",
    component: () => <div>Detail</div>,
  });
  const history = createMemoryHistory({ initialEntries: ["/"] });
  const router = createRouter({
    routeTree: rootRoute.addChildren([indexRoute, detailRoute]),
    history,
  });

  return render(
    <QueryClientProvider client={queryClient}>
      <RouterProvider router={router} />
    </QueryClientProvider>,
  );
}

describe("InsightsView", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });

  it("states the verdict and the server's conversation reach in the header", async () => {
    // 137 is what summing this page's `trace_count` gives, and it is wrong
    // three ways over. The header renders what the server aggregated instead.
    renderPage((() =>
      Promise.resolve(
        new Response(
          JSON.stringify({
            insights: [INSIGHT_HIGH, INSIGHT_LOW],
            total: 2,
            conversations: 41,
          }),
          { status: 200 },
        ),
      )) as typeof fetch);

    expect(await screen.findByText("it_support_agent")).toBeInTheDocument();
    expect(
      screen.getByText(/1 diagnosed, 1 undiagnosed · 41 trajectories affected/),
    ).toBeInTheDocument();
    expect(screen.getByText("7 occurrences")).toBeInTheDocument();
    expect(screen.getByText("1 occurrence")).toBeInTheDocument();
    expect(screen.queryByText(/137/)).toBeNull();
  });

  it("claims no reach when the agent sent no count", async () => {
    // An agent deployed before the field. No figure beats a zero stated as a
    // fact, and beats the page sum this replaced.
    renderPage((() =>
      Promise.resolve(
        new Response(
          JSON.stringify({ insights: [INSIGHT_HIGH, INSIGHT_LOW], total: 2 }),
          { status: 200 },
        ),
      )) as typeof fetch);

    expect(
      await screen.findByText(/1 diagnosed, 1 undiagnosed$/),
    ).toBeInTheDocument();
    expect(screen.queryByText(/affected/)).toBeNull();
  });

  it("agrees with itself about one trace", async () => {
    renderPage((() =>
      Promise.resolve(
        new Response(
          JSON.stringify({
            insights: [INSIGHT_HIGH],
            total: 1,
            conversations: 1,
          }),
          { status: 200 },
        ),
      )) as typeof fetch);

    expect(
      await screen.findByText(/1 trajectory affected/),
    ).toBeInTheDocument();
  });

  it("asks the engine to rank by impact, and draws the answer as it came", async () => {
    // Served lowest-impact first, which an engine ranking by impact would not
    // do -- so a list that reorders it is reordering a page rather than a
    // list, and the top of the ranking may be on a page it never fetched.
    let requestedUrl = "";
    renderPage(((input: RequestInfo | URL) => {
      requestedUrl = typeof input === "string" ? input : input.toString();
      return Promise.resolve(
        new Response(
          JSON.stringify({
            insights: [INSIGHT_LOW, INSIGHT_HIGH],
            total: 2,
          }),
          { status: 200 },
        ),
      );
    }) as typeof fetch);

    const cards = await screen.findAllByRole("link");
    const headlines = cards.map((c) => c.textContent);
    expect(headlines[0]).toContain("failed to call create_ticket");
    expect(requestedUrl).toContain("orderBy=impact");
  });

  it("tells a first-time user how to start root-cause analysis", async () => {
    // The Chat button is the only entry point to RCA, and its label does not
    // say so.
    renderPage((() =>
      Promise.resolve(
        new Response(JSON.stringify({ insights: [INSIGHT_LOW], total: 1 }), {
          status: 200,
        }),
      )) as typeof fetch);

    expect(
      await screen.findByText(/Nothing here has a root cause recorded yet/),
    ).toBeInTheDocument();
    expect(
      screen.getByText(/on an undiagnosed insight to run your first/),
    ).toBeInTheDocument();
  });

  it("drops the first-RCA hint once anything is diagnosed", async () => {
    renderPage((() =>
      Promise.resolve(
        new Response(
          JSON.stringify({ insights: [INSIGHT_HIGH, INSIGHT_LOW], total: 2 }),
          { status: 200 },
        ),
      )) as typeof fetch);

    expect(await screen.findByText("it_support_agent")).toBeInTheDocument();
    expect(screen.queryByText(/run your first root-cause analysis/)).toBeNull();
  });

  it("dismisses an insight when clicking Dismiss", async () => {
    const dismissed: string[] = [];
    renderPage((async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = typeof input === "string" ? input : input.toString();
      if (init?.method === "POST" && url.includes("/dismiss")) {
        dismissed.push(url);
        return new Response(JSON.stringify({ dismissed: true }), {
          status: 200,
        });
      }
      return new Response(
        JSON.stringify({
          insights: [INSIGHT_HIGH],
          total: 1,
        }),
        { status: 200 },
      );
    }) as typeof fetch);

    const button = await screen.findByRole("button", {
      name: /Dismiss insight/,
    });
    await userEvent.click(button);

    expect(dismissed.length).toBe(1);
    expect(dismissed[0]).toContain("/api/insights/ins-high/dismiss");
  });

  it("enters merge mode and selects candidates", async () => {
    renderPage((() =>
      Promise.resolve(
        new Response(
          JSON.stringify({
            insights: [INSIGHT_HIGH, INSIGHT_LOW],
            total: 2,
          }),
          { status: 200 },
        ),
      )) as typeof fetch);

    const mergeBtn = await screen.findByRole("button", {
      name: /Merge duplicates/,
    });
    await userEvent.click(mergeBtn);

    expect(
      screen.getByRole("region", { name: "Merge duplicate insights" }),
    ).toBeInTheDocument();

    const checkboxes = screen.getAllByRole("checkbox");
    expect(checkboxes.length).toBe(2);
    await userEvent.click(checkboxes[0]);
    await userEvent.click(checkboxes[1]);

    const keepButton = screen.getByRole("button", {
      name: /Keep: Priority value must be High, Medium, Low/,
    });
    expect(keepButton).not.toBeDisabled();
  });

  it("filters insights when clicking status pill", async () => {
    let requestedUrl = "";
    renderPage(((input: RequestInfo | URL) => {
      requestedUrl = typeof input === "string" ? input : input.toString();
      return Promise.resolve(
        new Response(
          JSON.stringify({
            insights: [INSIGHT_HIGH],
            total: 1,
          }),
          { status: 200 },
        ),
      );
    }) as typeof fetch);

    const recurringPill = await screen.findByRole("button", {
      name: "Recurring",
    });
    await userEvent.click(recurringPill);
    expect(requestedUrl).toContain("status=RECURRING");
  });

  it("does not render verified/unverified counters when insights list is empty", async () => {
    renderPage((() =>
      Promise.resolve(
        new Response(
          JSON.stringify({
            insights: [],
            total: 0,
          }),
          { status: 200 },
        ),
      )) as typeof fetch);

    expect(
      await screen.findByText(/No insights found for this filter/),
    ).toBeInTheDocument();
    expect(screen.queryByText(/verified/)).not.toBeInTheDocument();
    expect(screen.queryByText(/traces affected/)).not.toBeInTheDocument();
  });

  /** Renders the list with whatever props, against an empty result. */
  function renderEmpty(props: { runId?: string; embedded?: boolean }) {
    vi.stubGlobal("fetch", (() =>
      Promise.resolve(
        new Response(
          JSON.stringify({
            insights: [],
            total: 0,
          }),
          { status: 200 },
        ),
      )) as typeof fetch);
    const queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false, gcTime: 0 } },
    });
    const rootRoute = createRootRoute();
    const indexRoute = createRoute({
      getParentRoute: () => rootRoute,
      path: "/",
      component: () => <InsightsView {...props} />,
    });
    const history = createMemoryHistory({ initialEntries: ["/"] });
    const router = createRouter({
      routeTree: rootRoute.addChildren([indexRoute]),
      history,
    });

    render(
      <QueryClientProvider client={queryClient}>
        <RouterProvider router={router} />
      </QueryClientProvider>,
    );
  }

  it("brings no chrome of its own when embedded in another page", async () => {
    renderEmpty({ runId: "run-12345678", embedded: true });

    expect(
      await screen.findByText("Investigation run-1234 produced no insights."),
    ).toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: /Merge duplicates/ }),
    ).toBeNull();
    expect(screen.queryByRole("button", { name: "All" })).toBeNull();
    expect(screen.queryByRole("button", { name: /Refresh/ })).toBeNull();
  });

  it("keeps its tabs when a status matches nothing in an embedded run", async () => {
    // The run found insights, just none resolved: the tabs must survive the
    // empty answer, or "All" is unreachable without a reload.
    renderPage(
      ((input: RequestInfo | URL) => {
        const status = new URL(String(input), "http://x").searchParams.get(
          "status",
        );
        return Promise.resolve(
          new Response(
            JSON.stringify(
              status === "RESOLVED"
                ? { insights: [], total: 0 }
                : { insights: [INSIGHT_HIGH], total: 1 },
            ),
            { status: 200 },
          ),
        );
      }) as typeof fetch,
      { runId: "run-12345678", embedded: true },
    );
    const user = userEvent.setup();

    await user.click(await screen.findByRole("button", { name: "Resolved" }));

    expect(
      await screen.findByText(
        "None of investigation run-1234's insights are resolved.",
      ),
    ).toBeInTheDocument();
    expect(screen.queryByText(/produced no insights/)).toBeNull();

    await user.click(screen.getByRole("button", { name: "All" }));
    expect(await screen.findByText(INSIGHT_HIGH.diagnosis)).toBeInTheDocument();
  });

  /** `runId` and `embedded` were one prop, so a filtered list could not keep
   *  its own page. This is the case that separating them buys. */
  it("keeps its own chrome when filtered by run but not embedded", async () => {
    renderEmpty({ runId: "run-12345678" });

    // The reason for the filter still reaches the reader...
    expect(
      await screen.findByText("Investigation run-1234 produced no insights."),
    ).toBeInTheDocument();
    // ...and the page is still a page.
    expect(screen.getByRole("button", { name: "All" })).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: /Merge duplicates/ }),
    ).toBeInTheDocument();
  });

  it("says a filtered page is filtered, and offers the way out", async () => {
    // Six findings on a page that looks exactly like the unfiltered one reads
    // as "this agent has six".
    renderEmpty({ runId: "run-12345678" });

    expect(
      await screen.findByText(/Showing only what investigation/),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("link", { name: "Show all insights" }),
    ).toBeInTheDocument();
  });
});
