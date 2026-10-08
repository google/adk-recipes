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
 * The tour over the real dashboard: its own routes and pages, on a small
 * canned deployment. `tour-overlay.test.tsx` pins how the tour walks, over a
 * stand-in; this pins what it walks over -- that every step's page still
 * exists and still renders the element the step points at. A change anywhere
 * in the dashboard can break that without touching the tour: a renamed route,
 * a section that moved to another page, an anchor that went with a refactor.
 */
import { afterEach, describe, expect, it, vi } from "vitest";
import { act, render, waitFor } from "@testing-library/react";
import {
  createMemoryHistory,
  createRouter,
  RouterProvider,
} from "@tanstack/react-router";
import { routeTree } from "@/src/routeTree.gen";
import { counters } from "@/lib/__fixtures__/counters";
import { queryClient } from "@/lib/query-client";
import { endTour, showTourStep } from "@/lib/tour-state";
import { TOUR_STEPS } from "../tour-steps";

const INSIGHT = {
  insight_id: "ins-1",
  agent_name: "travel_agent",
  label: "books the flight for the wrong date",
  tool_name: "book_flight",
  status: "RECURRING",
  occurrence_count: 3,
  trace_count: 40,
  impact: 120,
  last_run_at: "2026-09-29T08:00:00Z",
  last_run_id: "run-1",
  diagnosis: "",
  confidence: 0,
};

const OCCURRENCE = {
  occurrence_id: "occ-1",
  run_id: "run-1",
  created_at: "2026-09-29T08:00:00Z",
  confidence: 0,
  diagnosis: "",
  label: INSIGHT.label,
  item_count: 3,
  trace_count: 3,
  cases_checked: 3,
  evidence_case_ids: ["case-1"],
  console_urls: {},
};

const RUN = {
  run_id: "run-1",
  status: "done",
  observed_agent_name: "travel_agent",
  trigger_type: "scheduled",
  created_at: "2026-09-29T08:00:00Z",
  elapsed_seconds: 60,
  metrics_passed: 8,
  metrics_failed: 2,
  metrics_errored: 0,
  counters: counters({
    traces_scanned: 10,
    traces_ingested: 10,
    traces_evaluated: 10,
  }),
  summary: {},
  events: [],
};

/** Answers the dashboard's reads for a deployment with one investigation and
 *  one insight; anything else gets an empty object. */
function cannedApi(input: RequestInfo | URL): Promise<Response> {
  const path = new URL(String(input), "http://dashboard").pathname;
  const body =
    path === "/api/health"
      ? { verdict: "watching", reason: "ran an hour ago" }
      : path === "/api/insights"
        ? { insights: [INSIGHT], total: 1 }
        : path.startsWith("/api/insights/")
          ? { insight: INSIGHT, occurrences: [OCCURRENCE] }
          : path === "/api/investigations"
            ? { runs: [RUN] }
            : path.startsWith("/api/investigations/")
              ? { run: RUN }
              : path === "/api/stats"
                ? { stats: {} }
                : path === "/api/daily"
                  ? { days: [] }
                  : path === "/api/config"
                    ? { config: {} }
                    : {};
  return Promise.resolve(new Response(JSON.stringify(body), { status: 200 }));
}

describe("the tour over the real dashboard", () => {
  afterEach(() => {
    act(() => endTour());
    queryClient.clear();
    vi.unstubAllGlobals();
  });

  it.each(TOUR_STEPS.map((step, index) => ({ target: step.target, index })))(
    "finds $target on the page its step opens",
    async ({ target, index }) => {
      vi.stubGlobal("fetch", cannedApi);
      const router = createRouter({
        routeTree,
        history: createMemoryHistory({ initialEntries: ["/"] }),
      });
      render(<RouterProvider router={router} />);

      act(() => showTourStep(index));
      await waitFor(
        () =>
          expect(
            document.querySelector(`[data-tour="${target}"]`),
          ).not.toBeNull(),
        { timeout: 5_000 },
      );
    },
  );
});
