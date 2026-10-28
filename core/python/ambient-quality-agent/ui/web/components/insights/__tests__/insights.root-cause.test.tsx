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

/** Tests for root-cause displays across list and detail insight views. */

import { afterEach, describe, expect, it, vi } from "vitest";
import { formatBugReport } from "../insight-detail";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import {
  createMemoryHistory,
  createRootRoute,
  createRoute,
  createRouter,
  RouterProvider,
} from "@tanstack/react-router";
import type { InsightOccurrence, InsightView, RootCause } from "@/lib/aqua-api";
import { InsightsView } from "../insights-view";
import { InsightDetailView } from "../insight-detail";
import { TopIssuesRail } from "../top-issues-rail";

const SUMMARY = "The refund step is phrased as a preference, not a constraint.";

const RECORD: RootCause = {
  root_cause_id: "rc-1",
  insight_id: "ins-1",
  occurrence_id: "occ-1",
  agent_revision: "rev-9",
  summary: SUMMARY,
  edits: [
    {
      path: "app/agent.py",
      start_line: 42,
      end_line: 44,
      before: "old line",
      after: "new line",
      rationale: "Make the step mandatory.",
    },
  ],
  created_at: "2026-01-02T03:04:05Z",
};

function insightView(over: Partial<InsightView> = {}): InsightView {
  return {
    insight_id: "ins-1",
    agent_name: "it_support_agent",
    label: "skipped the refund",
    tool_name: "issue_refund",
    status: "RECURRING",
    occurrence_count: 2,
    trace_count: 9,
    impact: 40,
    last_run_at: "2026-01-02T03:04:05Z",
    diagnosis: "",
    confidence: 0,
    ...over,
  };
}

function occurrence(over: Partial<InsightOccurrence> = {}): InsightOccurrence {
  return {
    occurrence_id: "occ-1",
    run_id: "run-1",
    created_at: "2026-01-02T03:04:05Z",
    confidence: 0,
    diagnosis: "",
    label: "skipped the refund",
    item_count: 1,
    trace_count: 1,
    cases_checked: 0,
    evidence_case_ids: [],
    console_urls: {},
    ...over,
  };
}

function renderWithRoutes(component: () => React.ReactNode) {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: 0 } },
  });
  const rootRoute = createRootRoute();
  const indexRoute = createRoute({
    getParentRoute: () => rootRoute,
    path: "/",
    component,
  });
  const listRoute = createRoute({
    getParentRoute: () => rootRoute,
    path: "/insights",
    component: () => <div>All insights list</div>,
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
  const router = createRouter({
    routeTree: rootRoute.addChildren([
      indexRoute,
      listRoute,
      detailRoute,
      chatRoute,
    ]),
    history: createMemoryHistory({ initialEntries: ["/"] }),
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <RouterProvider router={router} />
    </QueryClientProvider>,
  );
}

function stubInsights(insights: InsightView[]) {
  vi.stubGlobal(
    "fetch",
    (async () =>
      new Response(JSON.stringify({ insights, total: insights.length }), {
        status: 200,
      })) as typeof fetch,
  );
}

function stubDetail(
  insight: InsightView,
  occurrences: InsightOccurrence[],
  rootCauses: RootCause[] = [],
) {
  vi.stubGlobal(
    "fetch",
    (async () =>
      new Response(
        JSON.stringify({ insight, occurrences, root_causes: rootCauses }),
        { status: 200 },
      )) as typeof fetch,
  );
}

afterEach(() => vi.unstubAllGlobals());

describe("the insights list marker", () => {
  it("chips an insight that has a recorded root cause", async () => {
    stubInsights([insightView({ has_root_cause: true })]);
    renderWithRoutes(() => <InsightsView />);

    expect(await screen.findByTestId("root-cause-chip")).toBeInTheDocument();
  });

  it("leaves an undiagnosed insight unchipped", async () => {
    stubInsights([insightView({ has_root_cause: false })]);
    renderWithRoutes(() => <InsightsView />);

    expect(await screen.findByText("skipped the refund")).toBeInTheDocument();
    expect(screen.queryByTestId("root-cause-chip")).toBeNull();
  });

  it("stops calling a diagnosed insight undiagnosed", async () => {
    // Ensure list view recognizes recorded root causes as diagnosed without full summaries.
    stubInsights([insightView({ has_root_cause: true, diagnosis: "" })]);
    renderWithRoutes(() => <InsightsView />);

    await screen.findByTestId("root-cause-chip");
    expect(screen.queryByText("undiagnosed")).toBeNull();
    expect(screen.queryByText(/No diagnosis recorded/)).toBeNull();
    expect(screen.queryByText(/No root cause recorded yet/)).toBeNull();
  });

  it("still explains an insight that genuinely has no root cause recorded", async () => {
    stubInsights([insightView({ has_root_cause: false, diagnosis: "" })]);
    renderWithRoutes(() => <InsightsView />);

    // Says what is missing, not when the record was made.
    expect(
      await screen.findByText(/No root cause recorded yet/),
    ).toBeInTheDocument();
    expect(screen.queryByText(/[Ll]egacy/)).toBeNull();
  });
});

/** Verify consistent diagnosis indicators across header tallies, banners, card
 * badges, and detail views when an insight has a root cause without an
 * autorater diagnosis. */
describe("a recorded root cause with no autorater diagnosis", () => {
  const DIAGNOSED = insightView({ has_root_cause: true, diagnosis: "" });

  it("counts toward the header's diagnosed tally", async () => {
    stubInsights([DIAGNOSED]);
    renderWithRoutes(() => <InsightsView />);

    expect(
      await screen.findByText(/1 diagnosed, 0 undiagnosed/),
    ).toBeInTheDocument();
  });

  it("keeps the no-root-cause-phase banner off the page", async () => {
    stubInsights([DIAGNOSED]);
    renderWithRoutes(() => <InsightsView />);

    await screen.findByTestId("root-cause-chip");
    expect(screen.queryByText(/this engine runs no/)).toBeNull();
  });

  it("gets the diagnosed card border", async () => {
    stubInsights([DIAGNOSED]);
    renderWithRoutes(() => <InsightsView />);

    const chip = await screen.findByTestId("root-cause-chip");
    expect(chip.closest("a")).toHaveClass("border-emerald-500/40");
  });

  it("is not called undiagnosed in the sidebar rail", async () => {
    stubInsights([DIAGNOSED]);
    renderWithRoutes(() => <TopIssuesRail />);

    expect(await screen.findByText("skipped the refund")).toBeInTheDocument();
    expect(screen.queryByText("undiagnosed")).toBeNull();
  });

  it("is not called undiagnosed on its detail page", async () => {
    stubDetail(DIAGNOSED, [occurrence({ root_causes: [RECORD] })]);
    renderWithRoutes(() => <InsightDetailView insightId="ins-1" />);

    await screen.findByTestId("root-cause-panel");
    expect(screen.queryByText("undiagnosed")).toBeNull();
    expect(
      screen.queryByText("This occurrence has no recorded root cause."),
    ).toBeNull();
  });
});

describe("the top issues rail marker", () => {
  it("marks an insight that has a recorded root cause", async () => {
    stubInsights([insightView({ has_root_cause: true })]);
    renderWithRoutes(() => <TopIssuesRail />);

    expect(await screen.findByTestId("root-cause-icon")).toBeInTheDocument();
    expect(screen.queryByText(/undiagnosed/)).toBeNull();
  });

  it("calls an insight with neither diagnosis nor record undiagnosed", async () => {
    stubInsights([insightView({ has_root_cause: false, diagnosis: "" })]);
    renderWithRoutes(() => <TopIssuesRail />);

    expect(await screen.findByText(/· undiagnosed$/)).toBeInTheDocument();
    expect(screen.queryByTestId("root-cause-icon")).toBeNull();
  });
});

describe("the insight detail panel", () => {
  it("shows the record carried by the sighting it was written against", async () => {
    stubDetail(insightView({ has_root_cause: true }), [
      occurrence({ root_causes: [RECORD] }),
    ]);
    renderWithRoutes(() => <InsightDetailView insightId="ins-1" />);

    expect(await screen.findByTestId("root-cause-panel")).toBeInTheDocument();
    expect(screen.getByTestId("root-cause-record")).toBeInTheDocument();
    expect(screen.getByText(SUMMARY)).toBeInTheDocument();
    expect(screen.getByText("app/agent.py:42-44")).toBeInTheDocument();
    expect(screen.getByText("rev-9")).toBeInTheDocument();
  });

  it("draws the newest record and says how many it stands for", async () => {
    stubDetail(insightView({ has_root_cause: true }), [
      occurrence({
        occurrence_id: "occ-2",
        root_causes: [
          {
            ...RECORD,
            root_cause_id: "rc-2",
            occurrence_id: "occ-2",
            summary: "An older reading of the same defect.",
            created_at: "2025-06-01T00:00:00Z",
          },
        ],
      }),
      occurrence({ root_causes: [RECORD] }),
    ]);
    renderWithRoutes(() => <InsightDetailView insightId="ins-1" />);

    expect(await screen.findByText(SUMMARY)).toBeInTheDocument();
    expect(
      screen.queryByText("An older reading of the same defect."),
    ).toBeNull();
    expect(
      screen.getByText("Showing the latest of 2 records."),
    ).toBeInTheDocument();
  });

  it("shows a record whose sighting is not on this page of occurrences", async () => {
    // Support root-cause records preserved at the insight level when occurrence IDs differ.
    stubDetail(
      insightView({ has_root_cause: true }),
      [occurrence({ occurrence_id: "occ-fresh" })],
      [{ ...RECORD, occurrence_id: "occ-gone" }],
    );
    renderWithRoutes(() => <InsightDetailView insightId="ins-1" />);

    expect(await screen.findByTestId("root-cause-panel")).toBeInTheDocument();
    expect(screen.getByText(SUMMARY)).toBeInTheDocument();
  });

  it("shows no panel when nothing has been recorded", async () => {
    stubDetail(insightView(), [occurrence()]);
    renderWithRoutes(() => <InsightDetailView insightId="ins-1" />);

    expect(await screen.findByText("Latest occurrence")).toBeInTheDocument();
    expect(screen.queryByTestId("root-cause-panel")).toBeNull();
  });

  /** Verify the insight label, sighting autorater diagnosis, and root-cause
   * summary render in their distinct sections. */
  it("keeps the label, the sighting's reading and the summary distinct", async () => {
    const autoraterReading = "the category argument left the accepted set";
    stubDetail(insightView({ has_root_cause: true, diagnosis: "" }), [
      occurrence({ diagnosis: autoraterReading, root_causes: [RECORD] }),
    ]);
    renderWithRoutes(() => <InsightDetailView insightId="ins-1" />);

    expect(
      await screen.findByText("skipped the refund", { selector: "h1" }),
    ).toBeInTheDocument();
    expect(screen.getByText(autoraterReading)).toBeInTheDocument();
    expect(screen.getByTestId("root-cause-panel")).toHaveTextContent(SUMMARY);
    // Ensure the root-cause summary is confined to the dedicated panel.
    expect(screen.queryByText(SUMMARY, { selector: "h1" })).toBeNull();
    expect(screen.queryByText(/Triage signature/)).toBeNull();
  });
});

describe("the exported bug report", () => {
  // Include the recorded root cause in the bug report when no autorater diagnosis is present.
  it("carries the recorded cause when the autorater found none", () => {
    const report = formatBugReport({
      insight: insightView({ has_root_cause: true, diagnosis: "" }),
      occurrences: [occurrence({ root_causes: [RECORD] })],
      rootCause: RECORD,
    });

    expect(report).toContain(SUMMARY);
    expect(report).not.toContain("No automated diagnosis recorded.");
  });

  it("keeps the autorater diagnosis when there is one", () => {
    const report = formatBugReport({
      insight: insightView({
        has_root_cause: true,
        diagnosis: "Bad category.",
      }),
      occurrences: [occurrence({ root_causes: [RECORD] })],
      rootCause: RECORD,
    });

    expect(report).toContain("Bad category.");
    expect(report).not.toContain(SUMMARY);
  });
});
