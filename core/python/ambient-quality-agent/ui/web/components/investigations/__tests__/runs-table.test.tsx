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
 * The runs table Home's recent investigations and the investigations page
 * share. What each column says is pinned through the pages that draw it; these
 * cover the table's own behaviour and the wording of a run's outcome.
 */
import {
  RouterProvider,
  createMemoryHistory,
  createRootRoute,
  createRoute,
  createRouter,
} from "@tanstack/react-router";
import { fireEvent, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { Run } from "@/lib/aqua-api";
import { counters } from "@/lib/__fixtures__/counters";
import { RunsTable, runOutcome, runOutcomeTone } from "../runs-table";

const RUN: Run = {
  run_id: "abcdef1234",
  observed_agent_name: "travel_desk_agent",
  trigger_type: "scheduled",
  created_at: "2026-09-14T06:00:20Z",
  finished_at: "2026-09-14T06:04:00Z",
  window_start: "2026-09-14T00:00:00Z",
  window_end: "2026-09-14T06:00:00Z",
  status: "done",
  elapsed_seconds: 12,
  metrics_passed: 0,
  metrics_failed: 0,
  metrics_errored: 0,
  counters: counters({
    traces_scanned: 40,
    traces_ingested: 40,
    traces_eval_passed: 40,
  }),
  error: null,
};

/** The table on its own page, beside the page a row opens. */
function renderTable(runs: Run[], history?: Run[]) {
  const root = createRootRoute();
  const router = createRouter({
    routeTree: root.addChildren([
      createRoute({
        getParentRoute: () => root,
        path: "/",
        component: () => <RunsTable runs={runs} history={history} />,
      }),
      createRoute({
        getParentRoute: () => root,
        path: "/investigations/$runId",
      }),
    ]),
    history: createMemoryHistory({ initialEntries: ["/"] }),
  });
  // biome-ignore lint/suspicious/noExplicitAny: the test router is not the app's registered router type.
  render(<RouterProvider router={router as any} />);
  return router;
}

/** A run over the six hours before `created`, with `passed` of 40 passing. */
function sweep(id: string, created: string, passed: number): Run {
  const end = Date.parse(created) - 20_000;
  return {
    ...RUN,
    run_id: id,
    created_at: created,
    window_start: new Date(end - 6 * 3600_000).toISOString(),
    window_end: new Date(end).toISOString(),
    counters: counters({
      traces_scanned: 40,
      traces_ingested: 40,
      traces_eval_passed: passed,
      traces_eval_failed: 40 - passed,
    }),
  };
}

/** A row's cell under the named column, found through the header. */
function cellUnder(column: string, shortId: string): HTMLElement {
  const link = screen.getByRole("link", { name: shortId });
  const headers = [...link.closest("table")!.querySelectorAll("thead th")].map(
    (th) => th.textContent,
  );
  return link.closest("tr")!.querySelectorAll("td")[headers.indexOf(column)];
}

describe("RunsTable", () => {
  afterEach(() => vi.restoreAllMocks());

  it("opens the investigation from anywhere on its row", async () => {
    const router = renderTable([RUN]);
    await userEvent.click(await screen.findByText("100%"));
    expect(router.state.location.pathname).toBe("/investigations/abcdef1234");
  });

  it("stays put when the click only ends a text selection", async () => {
    // Selecting a run's figures to copy them is a drag, and the drag ends in a
    // click on the row.
    const router = renderTable([RUN]);
    const passed = await screen.findByText("100%");
    vi.spyOn(window, "getSelection").mockReturnValue({
      toString: () => "100%",
    } as Selection);
    await userEvent.click(passed);
    expect(router.state.location.pathname).toBe("/");
  });

  it("leaves the agent to the tab bar while every run observed the same one", async () => {
    renderTable([RUN, { ...RUN, run_id: "bbbbbbbb99" }]);
    await screen.findByRole("link", { name: "abcdef12" });
    expect(screen.queryByRole("columnheader", { name: "Agent" })).toBeNull();
    expect(screen.queryByText("travel_desk_agent")).toBeNull();
  });

  it("names each run's agent once the runs observed more than one", async () => {
    renderTable([
      RUN,
      { ...RUN, run_id: "bbbbbbbb99", observed_agent_name: "it_support_agent" },
    ]);
    expect(
      await screen.findByRole("columnheader", { name: "Agent" }),
    ).toBeInTheDocument();
    expect(screen.getByText("travel_desk_agent")).toBeInTheDocument();
    expect(screen.getByText("it_support_agent")).toBeInTheDocument();
  });

  it("notes a window only on the row whose window differs from the others'", async () => {
    renderTable([
      RUN,
      { ...RUN, run_id: "bbbbbbbb99" },
      // Started after a missed sweep, so its window reaches back further.
      { ...RUN, run_id: "cccccccc99", window_start: "2026-09-13T00:00:00Z" },
    ]);
    expect(await screen.findByText("30h window")).toBeInTheDocument();
    expect(screen.queryByText("6h window")).toBeNull();
  });

  it("gives the window on the start time's tooltip", async () => {
    renderTable([RUN]);
    await screen.findByRole("link", { name: "abcdef12" });
    expect(
      cellUnder("Started", "abcdef12").querySelector("[title]"),
    ).toHaveAttribute("title", expect.stringMatching(/^Window .+ – .+$/));
  });

  it("draws each run's trajectories on one scale, the busiest window longest", async () => {
    const quiet = sweep("quiet00001", "2026-09-14T00:00:20Z", 40);
    renderTable([
      RUN,
      {
        ...quiet,
        counters: counters({ ...quiet.counters, traces_scanned: 20 }),
      },
    ]);
    await screen.findByRole("link", { name: "abcdef12" });

    const busy = cellUnder("Trajectories", "abcdef12");
    const calm = cellUnder("Trajectories", "quiet000");
    expect(busy).toHaveTextContent("40");
    expect(calm).toHaveTextContent("20");
    expect(busy.querySelector("[style]")).toHaveStyle({ width: "100%" });
    expect(calm.querySelector("[style]")).toHaveStyle({ width: "50%" });
  });

  it("shows how far the pass rate moved since the investigation before", async () => {
    renderTable([
      sweep("newer00001", "2026-09-14T06:00:20Z", 30),
      sweep("older00001", "2026-09-14T00:00:20Z", 32),
    ]);
    await screen.findByRole("link", { name: "newer000" });

    // 75% after 80%, in red: a drop in the pass rate is bad news. The change
    // is in the percent the rate is written in, beside it.
    expect(cellUnder("Pass rate", "newer000")).toHaveTextContent(/^75%$/);
    expect(cellUnder("Change", "newer000")).toHaveTextContent(/^↓ down 5%$/);
    expect(within(cellUnder("Change", "newer000")).getByText("5%")).toHaveClass(
      "text-red-600",
    );
    // Nothing before it to compare with.
    expect(cellUnder("Pass rate", "older000")).toHaveTextContent(/^80%$/);
    expect(cellUnder("Change", "older000")).toHaveTextContent(/^—$/);
  });

  it("finds the investigation before in the history, past one that failed", async () => {
    const failed = {
      ...sweep("failed0001", "2026-09-14T03:00:20Z", 0),
      status: "failed",
      error: "boom",
    };
    const newer = sweep("newer00001", "2026-09-14T06:00:20Z", 36);
    // The row alone, as Home's last row or the investigations page's filter
    // leaves it: its predecessor is only in the list it was cut from.
    renderTable(
      [newer],
      [newer, failed, sweep("older00001", "2026-09-14T00:00:20Z", 32)],
    );
    await screen.findByRole("link", { name: "newer000" });

    expect(cellUnder("Change", "newer000")).toHaveTextContent(/^↑ up 10%$/);
    expect(
      within(cellUnder("Change", "newer000")).getByText("10%"),
    ).toHaveClass("text-teal-700");
  });

  it("leaves eval-service errors out of the pass rate, and names them beside it", async () => {
    // An outage of the eval service is not the agent getting worse.
    const errored = {
      ...sweep("errored001", "2026-09-14T06:00:20Z", 30),
      counters: counters({
        traces_scanned: 45,
        traces_ingested: 45,
        traces_eval_passed: 30,
        traces_eval_failed: 10,
        traces_eval_errored: 5,
      }),
    };
    renderTable([errored, sweep("older00001", "2026-09-14T00:00:20Z", 30)]);
    await screen.findByRole("link", { name: "errored0" });

    expect(cellUnder("Pass rate", "errored0")).toHaveTextContent(/^75%$/);
    // Counted plainly: the eval service's failure, not the agent's.
    expect(cellUnder("Errors", "errored0")).toHaveTextContent(/^5$/);
    expect(cellUnder("Errors", "errored0")).not.toHaveClass("text-red-600");
    // 75% both times: the five errors do not register as a drop.
    expect(cellUnder("Change", "errored0")).toHaveTextContent(/^0%$/);
  });

  it("gives a run whose every evaluation errored no rate, and says why", async () => {
    renderTable([
      {
        ...RUN,
        counters: counters({
          traces_scanned: 5,
          traces_ingested: 5,
          traces_eval_errored: 5,
        }),
      },
    ]);
    await screen.findByRole("link", { name: "abcdef12" });

    expect(cellUnder("Pass rate", "abcdef12")).toHaveTextContent(/^—$/);
    expect(cellUnder("Errors", "abcdef12")).toHaveTextContent(/^5$/);
  });

  it("compares a sweep only with sweeps, and draws no traffic bar for a manual run", async () => {
    // A manual run looks back over its own window and samples it its own way.
    const manual = {
      ...sweep("manual0001", "2026-09-14T03:00:20Z", 20),
      trigger_type: "manual",
    };
    renderTable([
      sweep("newer00001", "2026-09-14T06:00:20Z", 36),
      manual,
      sweep("older00001", "2026-09-14T00:00:20Z", 32),
    ]);
    await screen.findByRole("link", { name: "newer000" });

    // 90% against the sweep before the manual run (80%), not against its 50%.
    expect(cellUnder("Change", "newer000")).toHaveTextContent(/up 10%/);
    expect(cellUnder("Change", "manual00")).toHaveTextContent(/^—$/);
    expect(cellUnder("Trajectories", "manual00")).toHaveTextContent("40");
    expect(
      cellUnder("Trajectories", "manual00").querySelector("[style]"),
    ).toBeNull();
    expect(
      cellUnder("Trajectories", "newer000").querySelector("[style]"),
    ).not.toBeNull();
  });

  it("opens a new tab on a modified or middle click, as a link would", async () => {
    const open = vi.spyOn(window, "open").mockReturnValue(null);
    const router = renderTable([RUN]);
    const passed = await screen.findByText("100%");

    fireEvent.click(passed, { ctrlKey: true });
    fireEvent(passed, new MouseEvent("auxclick", { bubbles: true, button: 1 }));

    expect(open).toHaveBeenCalledTimes(2);
    expect(open).toHaveBeenCalledWith(
      "/investigations/abcdef1234",
      "_blank",
      "noopener",
    );
    expect(router.state.location.pathname).toBe("/");
  });

  it("keeps a middle press on the row from starting autoscroll, and leaves the link and the left button alone", async () => {
    renderTable([RUN]);
    const passed = await screen.findByText("100%");

    // fireEvent returns false once a handler has called preventDefault.
    expect(fireEvent.mouseDown(passed, { button: 1 })).toBe(false);
    expect(fireEvent.mouseDown(passed, { button: 0 })).toBe(true);
    expect(
      fireEvent.mouseDown(screen.getByRole("link", { name: "abcdef12" }), {
        button: 1,
      }),
    ).toBe(true);
  });

  it("compares a run with the previous sweep of its own agent", async () => {
    const other = (run: Run) => ({
      ...run,
      observed_agent_name: "it_support_agent",
    });
    renderTable([
      sweep("travelnew1", "2026-09-14T06:00:20Z", 30),
      other(sweep("supportmid", "2026-09-14T03:00:20Z", 10)),
      sweep("travelold1", "2026-09-14T00:00:20Z", 32),
      sweep("travelold2", "2026-09-13T18:00:20Z", 20),
    ]);
    await screen.findByRole("link", { name: "travelne" });

    // 75% after the travel agent's latest 80%: not after the support agent's
    // 25%, nor the travel agent's older 50%.
    expect(cellUnder("Change", "travelne")).toHaveTextContent(/down 5%/);
    expect(cellUnder("Change", "supportm")).toHaveTextContent(/^—$/);
  });

  it("shows no change when the rate rounds to the same percent", async () => {
    // 75.4% and 74.6% both read 75%, so an arrow saying 1 point would argue
    // with the figures beside it.
    const at = (id: string, created: string, passed: number) => ({
      ...sweep(id, created, 0),
      counters: counters({
        traces_scanned: 1000,
        traces_eval_passed: passed,
        traces_eval_failed: 1000 - passed,
      }),
    });
    renderTable([
      at("newer00001", "2026-09-14T06:00:20Z", 754),
      at("older00001", "2026-09-14T00:00:20Z", 746),
    ]);
    await screen.findByRole("link", { name: "newer000" });

    expect(cellUnder("Change", "newer000")).toHaveTextContent(/^0%$/);
  });

  it("keeps a thin traffic bar for a quiet sweep, and dashes a window with none", async () => {
    const at = (id: string, created: string, scanned: number) => {
      const run = sweep(id, created, 0);
      return {
        ...run,
        counters: counters({ ...run.counters, traces_scanned: scanned }),
      };
    };
    renderTable([
      at("busy000001", "2026-09-14T06:00:20Z", 1000),
      at("quiet00001", "2026-09-14T03:00:20Z", 1),
      { ...RUN, run_id: "empty00001", counters: counters() },
    ]);
    await screen.findByRole("link", { name: "busy0000" });

    expect(
      cellUnder("Trajectories", "quiet000").querySelector("[style]"),
    ).toHaveStyle({ width: "4%" });
    expect(cellUnder("Trajectories", "empty000")).toHaveTextContent(/^—$/);
  });

  it("notes no window on a lone row, having nothing to set it apart from", async () => {
    renderTable([{ ...RUN, window_start: "2026-09-13T00:00:00Z" }]);
    await screen.findByRole("link", { name: "abcdef12" });
    expect(screen.queryByText(/window$/)).toBeNull();
  });

  it("notes every window when no two rows share one", async () => {
    renderTable([
      RUN,
      { ...RUN, run_id: "bbbbbbbb99", window_start: "2026-09-13T00:00:00Z" },
      // A backfill, named by its bounds.
      {
        ...RUN,
        run_id: "cccccccc99",
        window_start: "2026-09-01T00:00:00Z",
        window_end: "2026-09-02T00:00:00Z",
      },
    ]);
    await screen.findByRole("link", { name: "abcdef12" });
    expect(screen.getByText("6h window")).toBeInTheDocument();
    expect(screen.getByText("30h window")).toBeInTheDocument();
    // The badge, not the row's hidden description, which also names it.
    const shown = within(cellUnder("Started", "cccccccc"))
      .getAllByText(/ – /)
      .filter((el) => !el.closest("[hidden]"));
    expect(shown).toHaveLength(1);
  });

  it("never notes a missing window, however the others read", async () => {
    renderTable([
      RUN,
      { ...RUN, run_id: "bbbbbbbb99" },
      { ...RUN, run_id: "nowindow99", window_start: null, window_end: null },
    ]);
    await screen.findByRole("link", { name: "nowindow" });
    expect(
      within(cellUnder("Started", "nowindow")).queryByText("—"),
    ).toBeNull();
  });

  it("leaves a click on the run's own link to the link", async () => {
    // A modified click on the link opens its own tab; the row must not open a
    // second one.
    const open = vi.spyOn(window, "open").mockReturnValue(null);
    renderTable([RUN]);
    fireEvent.click(await screen.findByRole("link", { name: "abcdef12" }), {
      ctrlKey: true,
    });
    expect(open).not.toHaveBeenCalled();
  });

  it("says on hover why a stalled run is called stalled", async () => {
    // Long past STALE_AFTER_MS, whatever today's date is.
    renderTable([{ ...RUN, status: "running", finished_at: null }]);
    expect(await screen.findByText("Stalled")).toHaveAttribute(
      "title",
      expect.stringMatching(
        /^No update for \d+[mhd]\. The record still says running/,
      ),
    );
  });
});

describe("runOutcome and runOutcomeTone", () => {
  const baseRun: Run = {
    run_id: "test-run",
    observed_agent_name: "test-agent",
    created_at: new Date().toISOString(),
    finished_at: new Date().toISOString(),
    status: "done",
    elapsed_seconds: 10,
    metrics_passed: 0,
    metrics_failed: 0,
    metrics_errored: 0,
    error: null,
  };

  it("mutes a skipped run", () => {
    const run: Run = { ...baseRun, status: "skipped" };
    expect(runOutcome(run)).toBe("skipped");
    expect(runOutcomeTone(run)).toBe("text-muted-foreground");
  });

  it("writes a failed run in red", () => {
    const run: Run = { ...baseRun, status: "failed" };
    expect(runOutcome(run)).toBe("failed");
    expect(runOutcomeTone(run)).toBe("text-red-600 dark:text-red-400");
  });

  it("calls a run that carries an error failed, in red, whatever its status", () => {
    const run: Run = { ...baseRun, status: "done", error: "Internal crash" };
    expect(runOutcome(run)).toBe("failed");
    expect(runOutcomeTone(run)).toBe("text-red-600 dark:text-red-400");
  });

  it("mutes a run that failed for want of telemetry, and calls it no data", () => {
    const noDataErr =
      "400 Field name service_version does not exist in STRUCT<gen_ai_conversation_id STRING>";
    const run: Run = { ...baseRun, status: "failed", error: noDataErr };
    expect(runOutcome(run)).toBe("no data");
    expect(runOutcomeTone(run)).toBe("text-muted-foreground");
  });

  it("calls a run nothing has updated in half an hour stalled, in amber", () => {
    // Created 2 hours ago with running status => stalled (> STALE_AFTER_MS = 30m)
    const staleTime = new Date(Date.now() - 2 * 3600 * 1000).toISOString();
    const run: Run = { ...baseRun, status: "running", created_at: staleTime };
    expect(runOutcome(run)).toBe("stalled");
    expect(runOutcomeTone(run)).toBe("text-amber-700 dark:text-amber-400");
  });

  it("names a run still in flight by its status, muted", () => {
    const freshTime = new Date().toISOString();
    const runningRun: Run = {
      ...baseRun,
      status: "running",
      created_at: freshTime,
    };
    expect(runOutcome(runningRun)).toBe("running");
    expect(runOutcomeTone(runningRun)).toBe("text-muted-foreground");

    const pendingRun: Run = {
      ...baseRun,
      status: "pending",
      created_at: freshTime,
    };
    expect(runOutcome(pendingRun)).toBe("pending");
    expect(runOutcomeTone(pendingRun)).toBe("text-muted-foreground");
  });

  it("gives a finished run's pass rate as a percent", () => {
    const run: Run = {
      ...baseRun,
      status: "done",
      counters: counters({
        traces_scanned: 10,
        traces_ingested: 10,
        traces_evaluated: 5,
        traces_eval_passed: 4,
        traces_eval_failed: 1,
      }),
    };
    expect(runOutcome(run)).toBe("80%");
    expect(runOutcomeTone(run)).toBe("text-muted-foreground");
  });

  it("says a finished run that evaluated nothing evaluated nothing", () => {
    const run: Run = {
      ...baseRun,
      status: "done",
      counters: counters(),
    };
    expect(runOutcome(run)).toBe("no trajectories evaluated");
    expect(runOutcomeTone(run)).toBe("text-muted-foreground");
  });
});
