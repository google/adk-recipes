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
import {
  createMemoryHistory,
  createRootRoute,
  createRoute,
  createRouter,
  RouterProvider,
} from "@tanstack/react-router";
import { InvestigationDetailView } from "../investigation-detail-view";
import type { InvestigationRecord } from "@/lib/aqua-api";
import { counters } from "@/lib/__fixtures__/counters";

const RECORD: InvestigationRecord = {
  run_id: "run-12345678",
  status: "done",
  observed_agent_name: "it_support_agent",
  trigger_type: "manual",
  created_at: "2026-08-28T18:35:34Z",
  elapsed_seconds: 42.5,
  metrics_passed: 10,
  metrics_failed: 2,
  metrics_errored: 0,
  counters: counters({
    traces_scanned: 50,
    traces_ingested: 50,
    traces_evaluated: 50,
    traces_eval_passed: 40,
    traces_eval_failed: 10,
    rubrics_generated: 70,
    clusters_created: 2,
    insights_created: 1,
    insights_recurring: 1,
  }),
  summary: {},
  events: [
    {
      run_id: "run-12345678",
      created_at: "2026-08-28T18:35:35Z",
      source: "triage",
      text: "**Triage:** Found 2 clusters.",
    },
  ],
};

function renderDetail(record: InvestigationRecord | null, error?: string) {
  vi.stubGlobal(
    "fetch",
    vi.fn().mockImplementation((url: string) => {
      if (url.includes("/api/insights")) {
        return Promise.resolve(
          new Response(JSON.stringify({ insights: [], total: 0 }), {
            status: 200,
          }),
        );
      }
      if (error) {
        return Promise.resolve(
          new Response(JSON.stringify({ error }), { status: 500 }),
        );
      }
      return Promise.resolve(
        new Response(JSON.stringify({ run: record }), { status: 200 }),
      );
    }),
  );

  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: 0 } },
  });

  const rootRoute = createRootRoute();
  const indexRoute = createRoute({
    getParentRoute: () => rootRoute,
    path: "/",
    component: () => <InvestigationDetailView runId="run-12345678" />,
  });
  const history = createMemoryHistory({ initialEntries: ["/"] });
  const router = createRouter({
    routeTree: rootRoute.addChildren([indexRoute]),
    history,
  });

  return render(
    <QueryClientProvider client={queryClient}>
      <RouterProvider router={router} />
    </QueryClientProvider>,
  );
}

const SELECTOR_SQL =
  "SELECT id\nFROM conversations\nWHERE <b>latency_ms</b> > 500";

describe("InvestigationDetailView", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });

  it("renders investigation metrics and counters", async () => {
    renderDetail({ ...RECORD, metrics_errored: 1 });

    expect(
      await screen.findByRole("heading", {
        level: 1,
        name: "Investigation run-12345678",
      }),
    ).toBeInTheDocument();
    expect(screen.getByText(/Observed agent:/)).toBeInTheDocument();
    expect(screen.getByText(/10 passed/)).toBeInTheDocument();
    expect(screen.getByText(/2 failed/)).toBeInTheDocument();
    expect(screen.getByText(/1 eval service error/)).toBeInTheDocument();
    expect(
      screen.getByText("Insights from this investigation"),
    ).toBeInTheDocument();
    expect(screen.getByText("Trajectories scanned")).toBeInTheDocument();
    expect(screen.getByText("Workflow narrative")).toBeInTheDocument();
  });

  it("renders plural eval errors and shows CustomMetricsPanel when only metrics_by_name is populated", async () => {
    renderDetail({
      ...RECORD,
      metrics_errored: 2,
      summary: {
        ...RECORD.summary,
        metrics_by_name: {
          ticket_completeness: {
            passed: 8,
            failed: 2,
            errored: 0,
          },
        },
      },
    });

    expect(
      await screen.findByText(/2 eval service errors/),
    ).toBeInTheDocument();
    expect(screen.getByText("Custom metrics")).toBeInTheDocument();
    expect(screen.getByText("ticket_completeness")).toBeInTheDocument();
  });

  it("colors the eval service error count but not the slash before it", async () => {
    // The slash before the error count is punctuation, like the one before
    // "failed", so it stays out of the red count.
    renderDetail({ ...RECORD, metrics_errored: 24 });

    expect(await screen.findByText("24 eval service errors")).toHaveClass(
      "text-destructive",
    );
  });

  it("shows the overrides a custom run was given, as text", async () => {
    renderDetail({
      ...RECORD,
      custom_overrides: {
        selector_sql: SELECTOR_SQL,
        session_review_focus: "Refund flows only",
      },
    });

    const sql = await screen.findByText(/FROM conversations/);
    // Ensure agent-authored markup renders as literal text without creating DOM elements.
    expect(sql.textContent).toBe(SELECTOR_SQL);
    expect(sql.querySelector("b")).toBeNull();
    expect(screen.getByText("Refund flows only")).toBeInTheDocument();
  });

  it("shows only the override that is set", async () => {
    renderDetail({
      ...RECORD,
      custom_overrides: {
        selector_sql: SELECTOR_SQL,
        session_review_focus: "",
      },
    });

    expect(await screen.findByText("Selector SQL")).toBeInTheDocument();
    expect(screen.queryByText(/Review focus:/)).not.toBeInTheDocument();
  });

  it("draws the investigation's own funnel, with a note written for one run", async () => {
    renderDetail(RECORD);

    expect(
      await screen.findByRole("heading", { name: "Pipeline" }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("img", {
        name: "Evaluated: 50 total. 40 passed, 10 failed",
      }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("img", {
        name: "Insights: 2 total. 1 new, 1 recurring",
      }),
    ).toBeInTheDocument();
    expect(
      screen.getByText(/this investigation's new and recurring insights/),
    ).toBeInTheDocument();
    expect(screen.queryByText(/added up/)).toBeNull();
  });

  it("says so when the investigation has no counts to draw", async () => {
    renderDetail({ ...RECORD, status: "running", counters: counters() });

    expect(
      await screen.findByText(
        "No counts yet: they are recorded when the investigation finishes.",
      ),
    ).toBeInTheDocument();
    expect(screen.queryByRole("img", { name: /^Evaluated:/ })).toBeNull();
  });

  it("names the header's figures as metric outcomes, apart from the funnel's trajectories", async () => {
    // 10 passed in the header and 40 in the funnel: one counts (trajectory,
    // metric) pairs, the other trajectories, and the page has to say which.
    renderDetail(RECORD);

    expect(await screen.findByText(/Metric outcomes/)).toBeInTheDocument();
    // Explained behind an (i), which a keyboard reaches, not a title.
    expect(
      screen.getByRole("button", { name: "About metric outcomes" }),
    ).toBeInTheDocument();
  });

  it("shows nothing about overrides for an ambient run", async () => {
    renderDetail(RECORD);

    expect(
      await screen.findByRole("heading", {
        level: 1,
        name: "Investigation run-12345678",
      }),
    ).toBeInTheDocument();
    expect(screen.queryByText("Selector SQL")).not.toBeInTheDocument();
    expect(screen.queryByText(/Review focus:/)).not.toBeInTheDocument();
  });
});
