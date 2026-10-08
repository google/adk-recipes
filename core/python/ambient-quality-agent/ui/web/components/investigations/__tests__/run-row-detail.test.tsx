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
 * What a run's row says beyond its counters: which agent it swept, the window
 * it covered, and the two conditions a lifecycle status cannot carry on its
 * own.
 *
 * `stalled` is the one worth pinning hardest. `runstate` stops believing a
 * `running` record after 30 minutes and re-enables Run investigation on that
 * basis; until the row said so too, the same judgement was acted on by the
 * button and invisible on the row, so the table read as a dashboard
 * contradicting itself.
 */
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import {
  RouterProvider,
  createMemoryHistory,
  createRootRoute,
  createRoute,
  createRouter,
} from "@tanstack/react-router";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { InvestigationsView } from "../investigations-view";

const ZERO = {
  traces_scanned: 0,
  traces_ingested: 0,
  traces_ingested_partial: 0,
  traces_ingested_failed: 0,
  traces_evaluated: 0,
  traces_eval_passed: 0,
  traces_eval_failed: 0,
  traces_eval_errored: 0,
  rubrics_generated: 0,
  findings_generated: 0,
  rubrics_errored: 0,
  rubrics_unclustered: 0,
  clusters_created: 0,
  clusters_verified: 0,
  clusters_rejected: 0,
  clusters_verify_skipped: 0,
  clusters_verify_failed: 0,
  insights_created: 0,
  insights_recurring: 0,
};

const run = (over: Record<string, unknown>) => ({
  run_id: "abcdef1234",
  observed_agent_name: "travel_desk_agent",
  // Comfortably older than STALE_AFTER_MS, so an unsettled status is stalled.
  created_at: "2026-09-14T06:00:00Z",
  finished_at: "2026-09-14T06:04:00Z",
  window_start: "2026-09-13T00:00:00Z",
  window_end: "2026-09-14T00:00:00Z",
  status: "done",
  elapsed_seconds: 12,
  metrics_passed: 0,
  metrics_failed: 0,
  metrics_errored: 0,
  counters: { ...ZERO },
  error: null,
  ...over,
});

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

/** The row carrying a given run id, found through its link. */
async function row(shortId = "abcdef12"): Promise<HTMLElement> {
  const link = await screen.findByRole("link", { name: shortId });
  return link.closest("tr")!;
}

describe("a run's row", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("names an agent-less record rather than leaving the cell blank", async () => {
    // The agent column shows only once the runs observed more than one.
    serve([
      run({ observed_agent_name: "" }),
      run({ run_id: "0ther00001", observed_agent_name: "it_support_agent" }),
    ]);
    expect(
      within(await row()).getByText("(unnamed agent)"),
    ).toBeInTheDocument();
  });

  it("names each run's agent on its row once there is more than one", async () => {
    serve([
      run({}),
      run({ run_id: "0ther00001", observed_agent_name: "it_support_agent" }),
    ]);
    expect(
      within(await row()).getByText("travel_desk_agent"),
    ).toBeInTheDocument();
    expect(
      within(await row("0ther000")).getByText("it_support_agent"),
    ).toBeInTheDocument();
  });

  it("gives the telemetry window it covered on hover, not how long it ran", async () => {
    // Rendered in the runner's locale, so the separator is what identifies it.
    serve([run({})]);
    expect(
      within(await row()).getByTitle(/^Window .+ – .+$/),
    ).toBeInTheDocument();
  });

  it("notes a window on the row only when it differs from the others'", async () => {
    const at = (id: string, hours: number) =>
      run({
        run_id: id,
        window_start: new Date(
          Date.parse("2026-09-14T06:00:00Z") - hours * 3600000,
        ).toISOString(),
        window_end: "2026-09-14T06:00:00Z",
      });
    serve([at("sixhoura01", 6), at("sixhourb01", 6), at("longone001", 30)]);
    expect(
      within(await row("longone0")).getByText("30h window"),
    ).toBeInTheDocument();
    expect(within(await row("sixhoura")).queryByText(/window/)).toBeNull();
  });

  it("gives no window when the record carries neither bound", async () => {
    serve([run({ window_start: null, window_end: null })]);
    expect(within(await row()).queryByTitle(/Window/)).toBeNull();
  });

  it("flags a sweep nothing has updated in long enough to believe", async () => {
    serve([run({ status: "running", finished_at: null })]);
    expect(within(await row()).getByText("Stalled")).toHaveAttribute(
      "title",
      expect.stringMatching(/^No update for /),
    );
  });

  it("does not call a finished sweep stalled, however old", async () => {
    serve([run({ status: "done" })]);
    expect(within(await row()).queryByText(/Stalled/)).toBeNull();
  });

  it("calls a failed run failed, and gives the error on hover", async () => {
    serve([run({ status: "failed", error: "BigQuery said no" })]);
    expect(within(await row()).getByText("Failed")).toHaveAttribute(
      "title",
      "BigQuery said no",
    );
  });

  it("reports a failed run as failed rather than as stalled", async () => {
    // Both conditions hold; an error explains itself, so it wins.
    serve([run({ status: "running", finished_at: null, error: "boom" })]);
    expect(within(await row()).getByText("Failed")).toHaveAttribute(
      "title",
      "boom",
    );
    expect(within(await row()).queryByText(/Stalled/)).toBeNull();
  });
});

describe("the only-with-failures filter", () => {
  afterEach(() => vi.unstubAllGlobals());

  const clean = run({
    run_id: "cleaaaaa11",
    counters: { ...ZERO, traces_evaluated: 4, traces_eval_passed: 4 },
  });
  const failing = run({
    run_id: "failfail22",
    counters: {
      ...ZERO,
      traces_evaluated: 4,
      traces_eval_passed: 3,
      traces_eval_failed: 1,
    },
  });

  it("hides a sweep that is provably clean", async () => {
    serve([clean, failing]);
    await screen.findByRole("link", { name: "cleaaaaa" });
    await userEvent.click(
      screen.getByRole("checkbox", { name: /only with failures/i }),
    );
    expect(screen.queryByRole("link", { name: "cleaaaaa" })).toBeNull();
    expect(screen.getByRole("link", { name: "failfail" })).toBeInTheDocument();
  });

  it("keeps a sweep it cannot judge while hiding an idle zero-trace sweep", async () => {
    // Scanned traces with nothing evaluated and no metric tallies: "cannot
    // confirm" is not "clean", so the run stays visible, whereas an idle sweep
    // that scanned zero traces is hidden.
    serve([
      run({ run_id: "unknown111", counters: { ...ZERO, traces_scanned: 4 } }),
      run({ run_id: "idleswee99", counters: { ...ZERO } }),
    ]);
    await screen.findByRole("link", { name: "unknown1" });
    await userEvent.click(
      screen.getByRole("checkbox", { name: /only with failures/i }),
    );
    expect(screen.getByRole("link", { name: "unknown1" })).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "idleswee" })).toBeNull();
  });

  it("says every sweep passed rather than showing an empty account", async () => {
    serve([clean]);
    await screen.findByRole("link", { name: "cleaaaaa" });
    await userEvent.click(
      screen.getByRole("checkbox", { name: /only with failures/i }),
    );
    expect(
      screen.getByText(/All investigations passed every metric/),
    ).toBeInTheDocument();
  });
});
