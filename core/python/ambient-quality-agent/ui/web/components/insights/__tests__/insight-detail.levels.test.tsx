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

// biome-ignore-all lint/style/noNonNullAssertion: a missing value fails the test either way; the assertion only narrows the type.

/**
 * The detail page reads in three levels: the heading, a summary anyone can act
 * on (what is wrong, its root cause, one example), and below it what a
 * developer needs, with the occurrence history folded away.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, within } from "@testing-library/react";
import {
  createMemoryHistory,
  createRootRoute,
  createRoute,
  createRouter,
  RouterProvider,
} from "@tanstack/react-router";
import type { InsightOccurrence, InsightView, RootCause } from "@/lib/aqua-api";
import { InsightDetailView } from "../insight-detail";

const SUMMARY = "Add the missing categories to the `file_expense` enum.";

const RECORD: RootCause = {
  root_cause_id: "rc-1",
  insight_id: "ins-1",
  occurrence_id: "occ-1",
  agent_revision: "rev-9",
  summary: SUMMARY,
  edits: [
    {
      path: "app/tools.py",
      start_line: 10,
      end_line: 12,
      before: "old line",
      after: "new line",
      rationale: "Accept the travel categories.",
    },
  ],
  created_at: "2026-01-02T03:04:05Z",
};

const finding = (n: number) => ({
  finding: {
    expected_behavior: `expected behavior ${n}`,
    actual_behavior: `actual behavior ${n}`,
  },
});

function insightView(over: Partial<InsightView> = {}): InsightView {
  return {
    insight_id: "ins-1",
    observed_agent_key: "expense_agent",
    label: "files expenses under a category the tool rejects",
    tool_name: "file_expense",
    status: "RECURRING",
    occurrence_count: 2,
    trajectory_count: 6,
    impact: 6,
    last_investigation_at: "2026-01-02T03:04:05Z",
    diagnosis: "",
    confidence: 0,
    ...over,
  };
}

function occurrence(over: Partial<InsightOccurrence> = {}): InsightOccurrence {
  return {
    occurrence_id: "occ-1",
    investigation_id: "inv-1",
    created_at: "2026-01-02T03:04:05Z",
    confidence: 0,
    diagnosis: "",
    label: "files expenses under a category the tool rejects",
    finding_count: 3,
    trajectory_count: 3,
    trajectories_checked: 0,
    evidence_trajectory_ids: [],
    console_urls: {},
    ...over,
  };
}

function renderDetail(
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
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: 0 } },
  });
  const rootRoute = createRootRoute();
  const routes = ["/insights", "/c", "/investigations/$investigationId"].map(
    (path) =>
      createRoute({
        getParentRoute: () => rootRoute,
        path,
        component: () => <div>{path}</div>,
      }),
  );
  const indexRoute = createRoute({
    getParentRoute: () => rootRoute,
    path: "/",
    component: () => <InsightDetailView insightId="ins-1" />,
  });
  const router = createRouter({
    routeTree: rootRoute.addChildren([indexRoute, ...routes]),
    history: createMemoryHistory({ initialEntries: ["/"] }),
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <RouterProvider router={router} />
    </QueryClientProvider>,
  );
}

beforeEach(() => window.localStorage.clear());
afterEach(() => vi.unstubAllGlobals());

describe("the insight summary", () => {
  it("says what is wrong once, from the latest sighting", async () => {
    const reading = "Six expenses used categories the tool does not accept.";
    renderDetail(insightView(), [occurrence({ diagnosis: reading })]);

    const summary = await screen.findByRole("region", { name: "Summary" });
    expect(within(summary).getByText("What's wrong")).toBeInTheDocument();
    expect(within(summary).getByText(reading)).toBeInTheDocument();
    expect(screen.getAllByText(reading)).toHaveLength(1);
  });

  it("prefers the insight's diagnosis over the latest sighting's", async () => {
    renderDetail(insightView({ diagnosis: "The insight's reading." }), [
      occurrence({ diagnosis: "The sighting's reading." }),
    ]);

    expect(
      await screen.findByText("The insight's reading."),
    ).toBeInTheDocument();
    expect(screen.queryByText("The sighting's reading.")).toBeNull();
  });

  it("renders code spans in the diagnosis as code", async () => {
    renderDetail(insightView(), [
      occurrence({ diagnosis: "The `create_ticket` declaration is empty." }),
    ]);

    expect((await screen.findByText("create_ticket")).tagName).toBe("CODE");
  });

  it("says when nothing describes the insight", async () => {
    renderDetail(insightView(), [occurrence()]);

    const summary = await screen.findByRole("region", { name: "Summary" });
    expect(within(summary).getByText("Nothing recorded yet.")).toBeVisible();
  });

  it("puts its fields under subheadings", async () => {
    renderDetail(insightView(), [occurrence({ examples: [finding(1)] })]);

    const region = await screen.findByRole("region", { name: "Summary" });
    for (const name of ["What's wrong", "Root cause", "Example finding"]) {
      expect(
        within(region).getByRole("heading", { level: 3, name }),
      ).toBeInTheDocument();
    }
  });

  it("shows the recorded root cause", async () => {
    renderDetail(
      insightView({ has_root_cause: true }),
      [occurrence()],
      [RECORD],
    );

    const summary = await screen.findByRole("region", { name: "Summary" });
    expect(within(summary).getByText("Root cause")).toBeInTheDocument();
    expect(within(summary).getByText("file_expense").tagName).toBe("CODE");
    expect(screen.getAllByText("file_expense")).toHaveLength(1);
  });

  it("lists each proposed edit's rationale as the suggested fix", async () => {
    renderDetail(
      insightView({ has_root_cause: true }),
      [occurrence()],
      [RECORD],
    );

    const summary = await screen.findByRole("region", { name: "Summary" });
    expect(within(summary).getByText("Suggested fix")).toBeInTheDocument();
    expect(
      within(summary).getByText("Accept the travel categories."),
    ).toBeVisible();
    // The diff itself stays with the developer details.
    expect(within(summary).queryByText("app/tools.py:10-12")).toBeNull();
  });

  it("offers no suggested fix when the record proposes no edits", async () => {
    renderDetail(
      insightView({ has_root_cause: true }),
      [occurrence()],
      [{ ...RECORD, edits: [] }],
    );

    await screen.findByRole("region", { name: "Summary" });
    expect(screen.queryByText("Suggested fix")).toBeNull();
  });

  it("does not call a record with an empty summary missing", async () => {
    renderDetail(
      insightView({ has_root_cause: true }),
      [occurrence()],
      [{ ...RECORD, summary: "" }],
    );

    const summary = await screen.findByRole("region", { name: "Summary" });
    expect(
      within(summary).queryByText(/No root cause recorded yet/),
    ).toBeNull();
    expect(
      within(summary).getByText(/Recorded without a summary/),
    ).toBeInTheDocument();
  });

  it("does not point at code changes a summary-less record lacks", async () => {
    renderDetail(
      insightView({ has_root_cause: true }),
      [occurrence()],
      [{ ...RECORD, summary: "", edits: [] }],
    );

    const summary = await screen.findByRole("region", { name: "Summary" });
    expect(
      within(summary).getByText("Recorded without a summary or code changes."),
    ).toBeInTheDocument();
  });

  it("says how to get a root cause when none is recorded", async () => {
    renderDetail(insightView(), [occurrence()]);

    const summary = await screen.findByRole("region", { name: "Summary" });
    expect(
      within(summary).getByText(/No root cause recorded yet/),
    ).toBeInTheDocument();
    // The diagnosis above is then only what the judge model saw.
    expect(within(summary).getByText(/what the judge model saw/)).toBeVisible();
  });

  it("shows one example and folds the other findings away", async () => {
    renderDetail(insightView(), [
      occurrence({ examples: [finding(1), finding(2), finding(3)] }),
    ]);

    const summary = await screen.findByRole("region", { name: "Summary" });
    expect(within(summary).getByText("actual behavior 1")).toBeVisible();
    const more = within(summary)
      .getByText("Show 2 more findings")
      .closest("details")!;
    expect(more).not.toHaveAttribute("open");
    expect(within(more).getByText("actual behavior 2")).toBeInTheDocument();
    expect(within(more).getByText("actual behavior 3")).not.toBeVisible();
    // The latest sighting does not repeat them.
    expect(screen.getAllByText("actual behavior 1")).toHaveLength(1);
  });

  it("offers no fold when there is only one finding", async () => {
    renderDetail(insightView(), [occurrence({ examples: [finding(1)] })]);

    await screen.findByText("actual behavior 1");
    expect(screen.queryByText(/more finding/)).toBeNull();
  });
});

describe("the developer level", () => {
  it("shows the proposed code changes unfolded, below the latest occurrence", async () => {
    renderDetail(
      insightView({ has_root_cause: true }),
      [occurrence()],
      [RECORD],
    );

    const panel = await screen.findByTestId("root-cause-panel");
    expect(panel.closest("details")).toBeNull();
    expect(within(panel).getByText("app/tools.py:10-12")).toBeVisible();
    const latest = screen.getByText("Latest occurrence");
    expect(
      latest.compareDocumentPosition(panel) & Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
  });

  it("folds the earlier occurrences away, and only those", async () => {
    renderDetail(
      insightView({ has_root_cause: true }),
      [
        occurrence({ occurrence_id: "occ-2", investigation_id: "inv-2" }),
        occurrence({ examples: [finding(9)] }),
      ],
      [RECORD],
    );

    const heading = await screen.findByRole("heading", {
      level: 2,
      name: /^History/,
    });
    const history = heading.closest("details")!;
    expect(history).not.toHaveAttribute("open");
    expect(within(history).getByText("actual behavior 9")).not.toBeVisible();
    expect(within(history).queryByTestId("root-cause-panel")).toBeNull();
  });

  it("keeps History open on the next insight once a reader opened it", async () => {
    window.localStorage.setItem("aqua.insight.history", "1");
    renderDetail(insightView(), [
      occurrence({ occurrence_id: "occ-2", investigation_id: "inv-2" }),
      occurrence({ examples: [finding(9)] }),
    ]);

    const heading = await screen.findByRole("heading", { name: /^History/ });
    expect(heading.closest("details")).toHaveAttribute("open");
    expect(screen.getByText("actual behavior 9")).toBeVisible();
  });

  it("keeps the other findings open once a reader opened them", async () => {
    window.localStorage.setItem("aqua.insight.more-findings", "1");
    renderDetail(insightView(), [
      occurrence({ examples: [finding(1), finding(2)] }),
    ]);

    expect(await screen.findByText("actual behavior 2")).toBeVisible();
  });
});
