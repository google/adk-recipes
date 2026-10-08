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
 * Every sighting carries its own rubrics -- what failed in that sweep. The
 * History cards render them; only the conclusion drawn across sightings, and
 * the notice about it, are dropped as repetition.
 */
import { describe, expect, it, vi, beforeEach } from "vitest";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, within } from "@testing-library/react";
import {
  createMemoryHistory,
  createRootRoute,
  createRoute,
  createRouter,
  RouterProvider,
} from "@tanstack/react-router";
import { InsightDetailView } from "../insight-detail";

const occurrence = (n: number, over: Record<string, unknown> = {}) => ({
  occurrence_id: `occ-${n}`,
  run_id: `run-0000000${n}`,
  created_at: `2026-09-0${n}T10:00:00Z`,
  confidence: 0,
  diagnosis: "",
  label: `sighting ${n}`,
  item_count: 2,
  trace_count: 2,
  cases_checked: 0,
  evidence_case_ids: [],
  console_urls: {},
  rubrics: [
    {
      rubric: {
        actual_behavior: `agent did the wrong thing in sweep ${n}.`,
        expected_behavior: `it should have done the right thing in sweep ${n}.`,
      },
    },
  ],
  ...over,
});

function renderDetail(occurrences: unknown[]) {
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
            occurrence_count: occurrences.length,
            trace_count: 9,
            impact: 9,
            last_run_at: "2026-09-03T10:00:00Z",
            diagnosis: "",
            confidence: 0,
          },
          occurrences,
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

describe("an insight's History sightings", () => {
  beforeEach(() => vi.restoreAllMocks());

  it("shows the evidence for every sighting, not only the newest", async () => {
    renderDetail([occurrence(3), occurrence(2), occurrence(1)]);

    expect(
      await screen.findByText(/agent did the wrong thing in sweep 3/),
    ).toBeInTheDocument();
    expect(
      screen.getByText(/agent did the wrong thing in sweep 2/),
    ).toBeInTheDocument();
    expect(
      screen.getByText(/agent did the wrong thing in sweep 1/),
    ).toBeInTheDocument();
  });

  it("sets the expected behaviour against the actual one, labelled", async () => {
    // Not run together into one sentence. The Finding model *is* these two
    // fields and the contrast is what a triager scans for, so each gets its
    // own row under its own label.
    renderDetail([occurrence(1)]);

    expect(
      await screen.findByText(
        "it should have done the right thing in sweep 1.",
      ),
    ).toBeInTheDocument();
    expect(
      screen.getByText("agent did the wrong thing in sweep 1."),
    ).toBeInTheDocument();
    expect(screen.getByText("Expected")).toBeInTheDocument();
    expect(screen.getByText("Actual")).toBeInTheDocument();
    // The flattened rendering this replaced.
    expect(screen.queryByText(/\. Expected: /)).toBeNull();
  });

  it("reads expected before actual, as an assertion failure prints it", async () => {
    renderDetail([occurrence(1)]);

    const labels = await screen.findAllByText(/^(Expected|Actual)$/);
    expect(labels.map((l) => l.textContent)).toEqual(["Expected", "Actual"]);
  });

  it("renders the one behaviour it has when the other is missing", async () => {
    renderDetail([
      occurrence(1, {
        rubrics: [{ rubric: { actual_behavior: "it timed out." } }],
      }),
    ]);

    expect(await screen.findByText("it timed out.")).toBeInTheDocument();
    expect(screen.getByText("Actual")).toBeInTheDocument();
    expect(screen.queryByText("Expected")).toBeNull();
  });

  it("falls back to an id when a finding carries neither behaviour", async () => {
    // A rubric shape we do not recognise is worth showing as its id rather
    // than as a blank row or a stack trace.
    renderDetail([occurrence(1, { rubrics: [{ eval_case_id: "case-77" }] })]);

    expect(await screen.findByText("Finding case-77")).toBeInTheDocument();
  });

  it("says the engine records no cause once, not on every sighting", async () => {
    renderDetail([occurrence(2), occurrence(1)]);

    await screen.findByText(/agent did the wrong thing in sweep 2/);
    expect(
      screen.getAllByText(/This occurrence has no recorded root cause/),
    ).toHaveLength(1);
  });

  it("does not repeat a diagnosis across the history", async () => {
    renderDetail([
      occurrence(2, { diagnosis: "The category enum drifted." }),
      occurrence(1, { diagnosis: "The category enum drifted." }),
    ]);

    await screen.findByText(/agent did the wrong thing in sweep 2/);
    expect(screen.getAllByText("The category enum drifted.")).toHaveLength(1);
    // The conclusion is shared; the per-sweep evidence is not.
    expect(
      screen.getByText(/agent did the wrong thing in sweep 1/),
    ).toBeInTheDocument();
  });

  it("renders a sighting that carries no rubrics without an empty list", async () => {
    renderDetail([occurrence(1, { rubrics: [] })]);

    const history = await screen.findByText(
      /This occurrence has no recorded root cause/,
    );
    expect(within(history.parentElement!).queryByRole("list")).toBeNull();
  });
});
