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
 * The runs table counts traces, the same unit the funnel above it draws: the
 * trajectories in the window, the pass rate, and the errors and insights, each
 * in a column of its own.
 */
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import {
  RouterProvider,
  createRootRoute,
  createRoute,
  createRouter,
  createMemoryHistory,
} from "@tanstack/react-router";
import { fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { InvestigationsView } from "../investigations-view";

/** Counters and metrics deliberately disagree: metrics count `(case, metric)`
 *  pairs, so they run several times ahead of the traces. */
const RUN = {
  run_id: "abcdef1234",
  observed_agent_name: "travel_desk_agent",
  created_at: "2026-09-14T06:00:00Z",
  finished_at: "2026-09-14T06:05:00Z",
  status: "done",
  elapsed_seconds: 12,
  metrics_passed: 900,
  metrics_failed: 300,
  metrics_errored: 12,
  counters: {
    traces_scanned: 4000,
    traces_ingested: 210,
    traces_ingested_partial: 6,
    traces_ingested_failed: 4,
    traces_evaluated: 205,
    traces_eval_passed: 150,
    traces_eval_failed: 50,
    traces_eval_errored: 2,
    clusters_created: 7,
    clusters_verified: 0,
    clusters_rejected: 0,
    clusters_verify_skipped: 0,
    clusters_verify_failed: 0,
    insights_created: 1,
    insights_recurring: 3,
  },
  error: null,
};

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

/** The one row's cell under the named column.
 *
 * Found through the header rather than by index, so adding a column does not
 * silently move every assertion onto its neighbour. */
async function cell(column: string): Promise<HTMLElement> {
  const link = await screen.findByRole("link", { name: "abcdef12" });
  const table = link.closest("table")!;
  const headers = [...table.querySelectorAll("thead th")].map((h) =>
    h.textContent!.trim(),
  );
  const index = headers.indexOf(column);
  expect(
    index,
    `no "${column}" column in ${headers.join(", ")}`,
  ).toBeGreaterThan(-1);
  return link.closest("tr")!.querySelectorAll("td")[index];
}

/** What the one row reads under the named column. */
async function read(column: string): Promise<string> {
  return (await cell(column)).textContent!.trim();
}

describe("the runs table's counters", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("counts evaluated traces, not (case, metric) outcomes", async () => {
    serve([RUN]);

    // 150 / 50 / 2, not 900 / 300 / 12; the two eval-service errors are counted
    // apart rather than as failures.
    expect(await read("Pass rate")).toBe("75%");
    expect(await read("Errors")).toBe("2");
  });

  it("gives the trajectories in the run's window", async () => {
    serve([RUN]);

    // In the runner's locale, as the table writes it.
    expect(await read("Trajectories")).toBe((4000).toLocaleString());
  });

  it("shows what the run produced", async () => {
    serve([RUN]);

    expect(await read("New")).toBe("1");
    expect(await read("Recurring")).toBe("3");
  });

  it("counts a clean run's errors and insights as 0", async () => {
    serve([
      {
        ...RUN,
        counters: {
          traces_scanned: 40,
          traces_ingested: 40,
          traces_ingested_partial: 0,
          traces_ingested_failed: 0,
          traces_eval_passed: 40,
          traces_eval_failed: 0,
          traces_eval_errored: 0,
          clusters_created: 0,
          clusters_verified: 0,
          clusters_rejected: 0,
          clusters_verify_skipped: 0,
          clusters_verify_failed: 0,
        },
      },
    ]);

    expect(await read("Pass rate")).toBe("100%");
    expect(await read("Errors")).toBe("0");
    // Measured zeros, not dashes: the run evaluated and found nothing.
    expect(await read("New")).toBe("0");
    expect(await read("Recurring")).toBe("0");
  });

  it("dashes a run whose counters are all zero rather than printing noughts", async () => {
    // A sweep still running, or a record written before the counters existed:
    // all-zero is indistinguishable from "saw nothing", so neither claims a
    // measurement.
    serve([
      { ...RUN, counters: { traces_eval_passed: 0, traces_eval_failed: 0 } },
    ]);

    expect(await read("Pass rate")).toBe("No trajectories evaluated");
    expect(await read("Errors")).toBe("—");
    expect(await read("New")).toBe("—");
    expect(await read("Recurring")).toBe("—");
  });

  it("dashes a record that carries no counters at all", async () => {
    const { counters, ...withoutCounters } = RUN;
    void counters;
    serve([withoutCounters]);

    expect(await read("New")).toBe("—");
    expect(await read("Trajectories")).toBe("—");
  });

  it("hides idle done sweeps when 'Only with failures' is checked", async () => {
    const idleRun = {
      ...RUN,
      run_id: "0ff3d16399",
      status: "done",
      error: null,
      metrics_passed: 0,
      metrics_failed: 0,
      metrics_errored: 0,
      counters: {
        traces_scanned: 0,
        traces_ingested: 0,
        traces_ingested_partial: 0,
        traces_ingested_failed: 0,
        traces_evaluated: 0,
        traces_eval_passed: 0,
        traces_eval_failed: 0,
        traces_eval_errored: 0,
        clusters_created: 0,
        clusters_verified: 0,
        clusters_rejected: 0,
        clusters_verify_skipped: 0,
        clusters_verify_failed: 0,
        insights_created: 0,
        insights_recurring: 0,
      },
    };
    serve([RUN, idleRun]);

    expect(await screen.findByRole("link", { name: "abcdef12" })).toBeTruthy();
    expect(await screen.findByRole("link", { name: "0ff3d163" })).toBeTruthy();

    fireEvent.click(screen.getByLabelText(/only with failures/i));

    expect(screen.queryByRole("link", { name: "0ff3d163" })).toBeNull();
    expect(screen.getByRole("link", { name: "abcdef12" })).toBeTruthy();
  });

  it("compares a failing run with the clean one before it, which the filter hides", async () => {
    serve([
      { ...RUN, counters: { traces_eval_passed: 3, traces_eval_failed: 1 } },
      {
        ...RUN,
        run_id: "c1ean00001",
        created_at: "2026-09-14T00:00:00Z",
        metrics_failed: 0,
        metrics_errored: 0,
        counters: { traces_eval_passed: 4, traces_eval_failed: 0 },
      },
    ]);
    await screen.findByRole("link", { name: "c1ean000" });

    fireEvent.click(screen.getByLabelText(/only with failures/i));

    expect(screen.queryByRole("link", { name: "c1ean000" })).toBeNull();
    expect(await read("Pass rate")).toBe("75%");
    expect(await read("Change")).toMatch(/down 25%/);
  });

  it("explains when no investigations in the view have failures", async () => {
    const idleRun = {
      ...RUN,
      run_id: "0ff3d16399",
      status: "done",
      error: null,
      metrics_passed: 0,
      metrics_failed: 0,
      metrics_errored: 0,
      counters: {
        traces_scanned: 0,
        traces_ingested: 0,
        traces_evaluated: 0,
        traces_eval_passed: 0,
        traces_eval_failed: 0,
        traces_eval_errored: 0,
      },
    };
    serve([idleRun]);
    expect(await screen.findByRole("link", { name: "0ff3d163" })).toBeTruthy();

    fireEvent.click(screen.getByLabelText(/only with failures/i));

    expect(
      screen.getByText(/to see clean and idle investigations/i),
    ).toBeTruthy();
  });
});
