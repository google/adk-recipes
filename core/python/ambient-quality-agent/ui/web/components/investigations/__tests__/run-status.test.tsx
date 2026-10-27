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
 * The runs table used to read `rca_status`, which nothing has written since
 * RCA was removed in #77, so every row fell through to "done" -- including the
 * sweep that was still running. These pin the lifecycle states apart. A run
 * that has not finished with a count to show says its state where the count
 * would be, under Pass rate.
 */
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import {
  RouterProvider,
  createRootRoute,
  createRoute,
  createRouter,
  createMemoryHistory,
} from "@tanstack/react-router";
import { render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { InvestigationsView } from "../investigations-view";

const run = (over: Record<string, unknown>) => ({
  run_id: "abcdef1234",
  observed_agent_name: "travel_desk_agent",
  // Now, so an unsettled status is still believed rather than stalled.
  created_at: new Date().toISOString(),
  finished_at: null,
  status: "done",
  elapsed_seconds: 12,
  metrics_passed: 0,
  metrics_failed: 0,
  metrics_errored: 0,
  error: null,
  ...over,
});

/** Answers every dashboard read; `runs` is what the table is built from. */
function serve(runs: Record<string, unknown>[]) {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string) => {
      const body = url.includes("/api/investigations")
        ? { runs }
        : url.includes("/api/stats")
          ? { stats: {} }
          : { days: [] };
      return new Response(JSON.stringify(body), { status: 200 });
    }),
  );
  const root = createRootRoute();
  const children = ["/investigations", "/investigations/$runId"].map((path) =>
    createRoute({
      getParentRoute: () => root,
      path,
      ...(path === "/investigations" ? { component: InvestigationsView } : {}),
    }),
  );
  const router = createRouter({
    routeTree: root.addChildren(children),
    history: createMemoryHistory({ initialEntries: ["/investigations"] }),
  });
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={qc}>
      {/* biome-ignore lint/suspicious/noExplicitAny: the test router is not the app's registered router type. */}
      <RouterProvider router={router as any} />
    </QueryClientProvider>,
  );
}

/** What the one row says under Pass rate, read off the row itself: scoped,
 *  because a state of "failed" would collide with a page-wide query. */
async function statusCell(): Promise<string> {
  const link = await screen.findByRole("link", { name: "abcdef12" });
  const table = link.closest("table")!;
  const index = [...table.querySelectorAll("thead th")].findIndex(
    (th) => th.textContent === "Pass rate",
  );
  expect(index, "no Pass rate column").toBeGreaterThan(-1);
  return link.closest("tr")!.querySelectorAll("td")[index].textContent!.trim();
}

describe("the state a run's row names", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("says a sweep is still running rather than calling it done", async () => {
    serve([run({ status: "running", finished_at: null })]);
    expect(await statusCell()).toBe("Running");
  });

  it("says a sweep is pending rather than calling it done", async () => {
    serve([run({ status: "pending", finished_at: null })]);
    expect(await statusCell()).toBe("Pending");
  });

  it("gives a finished sweep's outcome rather than a state", async () => {
    serve([
      run({
        status: "done",
        finished_at: "2026-09-14T06:04:00Z",
        counters: { traces_eval_passed: 3, traces_eval_failed: 1 },
      }),
    ]);
    expect(await statusCell()).toBe("75%");
  });

  it("ignores an rca_status the engine still passes through", async () => {
    // `format_run` lifts it from the stored summary, so it can be present on
    // an old record. It is not the run's lifecycle state.
    serve([run({ status: "running", rca_status: "executed" })]);
    expect(await statusCell()).toBe("Running");
  });

  it("calls a record with no status but an error failed", async () => {
    serve([run({ status: "", error: "BigQuery said no" })]);
    expect(await statusCell()).toBe("Failed");
  });
});
