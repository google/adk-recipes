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
 * The detail pane against the payload our engine actually sends.
 *
 * Every other test in this directory builds its fixtures from `insightView()`
 * and `occurrence()`, which fill the finding fields in. A sweep sends those
 * keys absent -- nothing has diagnosed the issue yet -- and absent, not zero,
 * is what the pane blanked on: `insightQuery` was the one query that skipped
 * `withDefaults`, so `evidence_case_ids.length` ran against `undefined` and
 * the whole view went to the error boundary.
 *
 * So this renders the real component over a raw payload rather than a fixture.
 * A defaulter that stops covering a field fails here, where a fixture-based
 * test would keep passing.
 */
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import {
  RouterProvider,
  createMemoryHistory,
  createRootRoute,
  createRoute,
  createRouter,
} from "@tanstack/react-router";
import { render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { InsightDetailView } from "../insight-detail";

/** What `/api/insights/{id}` returns from our engine: no diagnosis, no
 * confidence, no source refs, no cases_checked. `evidence_case_ids` is there
 * because `ui/app.py` projects it from the `trajectory_ids` #189 writes. */
const OUR_PAYLOAD = {
  insight: {
    insight_id: "ins-1",
    agent_name: "travel_desk_agent",
    label: "calls file_expense with a category outside the accepted set",
    status: "RECURRING",
    occurrence_count: 110,
    trace_count: 592,
    last_run_id: "run-9",
    last_run_at: "2026-09-10T06:00:00Z",
  },
  occurrences: [
    {
      occurrence_id: "o-1",
      run_id: "run-9",
      created_at: "2026-09-10T06:00:00Z",
      label: "calls file_expense with a category outside the accepted set",
      item_count: 6,
      trace_count: 6,
      trajectory_ids: ["case-91f92b", "case-41cf06"],
      evidence_case_ids: ["case-91f92b", "case-41cf06"],
      rubrics: [],
    },
  ],
};

function renderDetail(payload: unknown) {
  vi.stubGlobal(
    "fetch",
    vi.fn(async () => new Response(JSON.stringify(payload), { status: 200 })),
  );
  const root = createRootRoute();
  const paths = ["/", "/c", "/insights", "/investigations/$runId"];
  const [index, ...rest] = paths.map((path) =>
    createRoute({
      getParentRoute: () => root,
      path,
      ...(path === "/"
        ? { component: () => <InsightDetailView insightId="ins-1" /> }
        : {}),
    }),
  );
  const caseRoute = createRoute({
    getParentRoute: () => root,
    path: "/investigations/$runId/cases/$caseId",
    component: () => <div>Case</div>,
  });
  const router = createRouter({
    routeTree: root.addChildren([index, ...rest, caseRoute]),
    history: createMemoryHistory({ initialEntries: ["/"] }),
  });
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={qc}>
      {/* biome-ignore lint/suspicious/noExplicitAny: the test router is not the app's registered router type. */}
      <RouterProvider router={router as any} />
    </QueryClientProvider>,
  );
}

afterEach(() => vi.unstubAllGlobals());

describe("the detail pane on an engine with no dig phase", () => {
  it("renders instead of blanking", async () => {
    renderDetail(OUR_PAYLOAD);

    expect(
      await screen.findByText(
        "calls file_expense with a category outside the accepted set",
        { selector: "h1" },
      ),
    ).toBeInTheDocument();
    expect(await screen.findByText("Latest occurrence")).toBeInTheDocument();
  });

  it("says the finding is undiagnosed rather than showing a false 0%", async () => {
    renderDetail(OUR_PAYLOAD);
    // Twice: once in the header, once on the sighting.
    expect(await screen.findAllByText("Undiagnosed")).toHaveLength(2);
  });

  it("says the sighting has no recorded cause", async () => {
    renderDetail(OUR_PAYLOAD);
    expect(
      await screen.findByText(/This occurrence has no recorded root cause/),
    ).toBeInTheDocument();
  });

  it("shows no confidence, because nothing measures one", async () => {
    renderDetail(OUR_PAYLOAD);
    await screen.findByText("Latest occurrence");
    // Not "0%", which reads as a measurement that came out at zero.
    expect(screen.queryByText(/%/)).toBeNull();
  });

  it("counts the traces once, not twice", async () => {
    // `cases_checked` is populated from `trace_count`, so the fork's separate
    // "N independently checked" chip would print the same number again beside
    // "N trace(s)" -- and claim a second check we never ran.
    renderDetail(OUR_PAYLOAD);
    expect(await screen.findByText("6 trajectories")).toBeInTheDocument();
    expect(screen.queryByText(/independently checked/)).toBeNull();
  });

  it("lists the conversations behind the sighting", async () => {
    // The whole return on projecting `trajectory_ids`: without it this section
    // does not render and the sighting is a count with nothing behind it.
    renderDetail(OUR_PAYLOAD);
    expect(await screen.findByText("Trajectories")).toBeInTheDocument();
    expect(await screen.findByText("110 occurrences")).toBeInTheDocument();
    expect(await screen.findByText("case-91f92b")).toBeInTheDocument();
  });

  it("renders even when the engine sends no evidence at all", async () => {
    // An older occurrence, written before #189: no trajectory ids to project,
    // so `ui/app.py` sends an empty list and the section is simply absent.
    renderDetail({
      insight: { ...OUR_PAYLOAD.insight },
      occurrences: [
        { occurrence_id: "o-1", created_at: "2026-09-10T06:00:00Z" },
      ],
    });

    expect(await screen.findByText("Latest occurrence")).toBeInTheDocument();
    expect(screen.queryByText("Trajectories")).toBeNull();
  });
});
