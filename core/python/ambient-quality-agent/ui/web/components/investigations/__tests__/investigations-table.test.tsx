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
 * The investigations table Home's recent investigations and the investigations
 * page share. What each column says is pinned through the pages that draw it;
 * these cover the table's own behaviour and the wording of an investigation's
 * outcome.
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
import type { InvestigationListItem } from "@/lib/aqua-api";
import { counters } from "@/lib/__fixtures__/counters";
import {
  InvestigationsTable,
  formatInvestigationOutcome,
  resolveInvestigationOutcomeTone,
} from "../investigations-table";

const INVESTIGATION: InvestigationListItem = {
  investigation_id: "abcdef1234",
  observed_agent_key: "travel_desk_agent",
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
    trajectories_scanned: 40,
    trajectories_ingested: 40,
    trajectories_eval_passed: 40,
  }),
  error: null,
};

/** The table on its own page, beside the page a row opens. */
function renderTable(
  investigations: InvestigationListItem[],
  history?: InvestigationListItem[],
) {
  const root = createRootRoute();
  const router = createRouter({
    routeTree: root.addChildren([
      createRoute({
        getParentRoute: () => root,
        path: "/",
        component: () => (
          <InvestigationsTable
            investigations={investigations}
            history={history}
          />
        ),
      }),
      createRoute({
        getParentRoute: () => root,
        path: "/investigations/$investigationId",
      }),
    ]),
    history: createMemoryHistory({ initialEntries: ["/"] }),
  });
  // biome-ignore lint/suspicious/noExplicitAny: the test router is not the app's registered router type.
  render(<RouterProvider router={router as any} />);
  return router;
}

/** An investigation over the six hours before `created`, with `passed` of
 *  40 passing. */
function sweep(
  id: string,
  created: string,
  passed: number,
): InvestigationListItem {
  const end = Date.parse(created) - 20_000;
  return {
    ...INVESTIGATION,
    investigation_id: id,
    created_at: created,
    window_start: new Date(end - 6 * 3600_000).toISOString(),
    window_end: new Date(end).toISOString(),
    counters: counters({
      trajectories_scanned: 40,
      trajectories_ingested: 40,
      trajectories_eval_passed: passed,
      trajectories_eval_failed: 40 - passed,
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

describe("InvestigationsTable", () => {
  afterEach(() => vi.restoreAllMocks());

  it("opens the investigation from anywhere on its row", async () => {
    const router = renderTable([INVESTIGATION]);
    await userEvent.click(await screen.findByText("100%"));
    expect(router.state.location.pathname).toBe("/investigations/abcdef1234");
  });

  it("stays put when the click only ends a text selection", async () => {
    // Selecting an investigation's figures to copy them is a drag, and the drag
    // ends in a click on the row.
    const router = renderTable([INVESTIGATION]);
    const passed = await screen.findByText("100%");
    vi.spyOn(window, "getSelection").mockReturnValue({
      toString: () => "100%",
    } as Selection);
    await userEvent.click(passed);
    expect(router.state.location.pathname).toBe("/");
  });

  it("leaves the agent to the tab bar while every investigation observed the same one", async () => {
    renderTable([
      INVESTIGATION,
      { ...INVESTIGATION, investigation_id: "bbbbbbbb99" },
    ]);
    await screen.findByRole("link", { name: "abcdef12" });
    expect(screen.queryByRole("columnheader", { name: "Agent" })).toBeNull();
    expect(screen.queryByText("travel_desk_agent")).toBeNull();
  });

  it("names each investigation's agent once the investigations observed more than one", async () => {
    renderTable([
      INVESTIGATION,
      {
        ...INVESTIGATION,
        investigation_id: "bbbbbbbb99",
        observed_agent_key: "it_support_agent",
      },
    ]);
    expect(
      await screen.findByRole("columnheader", { name: "Agent" }),
    ).toBeInTheDocument();
    expect(screen.getByText("travel_desk_agent")).toBeInTheDocument();
    expect(screen.getByText("it_support_agent")).toBeInTheDocument();
  });

  it("notes a window only on the row whose window differs from the others'", async () => {
    renderTable([
      INVESTIGATION,
      { ...INVESTIGATION, investigation_id: "bbbbbbbb99" },
      // Started after a missed sweep, so its window reaches back further.
      {
        ...INVESTIGATION,
        investigation_id: "cccccccc99",
        window_start: "2026-09-13T00:00:00Z",
      },
    ]);
    expect(await screen.findByText("30h window")).toBeInTheDocument();
    expect(screen.queryByText("6h window")).toBeNull();
  });

  it("gives the window on the start time's tooltip", async () => {
    renderTable([INVESTIGATION]);
    await screen.findByRole("link", { name: "abcdef12" });
    expect(
      cellUnder("Started", "abcdef12").querySelector("[title]"),
    ).toHaveAttribute("title", expect.stringMatching(/^Window .+ – .+$/));
  });

  it("draws each investigation's trajectories on one scale, the busiest window longest", async () => {
    const quiet = sweep("quiet00001", "2026-09-14T00:00:20Z", 40);
    renderTable([
      INVESTIGATION,
      {
        ...quiet,
        counters: counters({ ...quiet.counters, trajectories_scanned: 20 }),
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
        trajectories_scanned: 45,
        trajectories_ingested: 45,
        trajectories_eval_passed: 30,
        trajectories_eval_failed: 10,
        trajectories_eval_errored: 5,
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

  it("gives an investigation whose every evaluation errored no rate, and says why", async () => {
    renderTable([
      {
        ...INVESTIGATION,
        counters: counters({
          trajectories_scanned: 5,
          trajectories_ingested: 5,
          trajectories_eval_errored: 5,
        }),
      },
    ]);
    await screen.findByRole("link", { name: "abcdef12" });

    expect(cellUnder("Pass rate", "abcdef12")).toHaveTextContent(/^—$/);
    expect(cellUnder("Errors", "abcdef12")).toHaveTextContent(/^5$/);
  });

  it("compares a sweep only with sweeps, and draws no traffic bar for a manual investigation", async () => {
    // A manual investigation looks back over its own window and samples it its
    // own way.
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

    // 90% against the sweep before the manual investigation (80%), not against
    // its 50%.
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
    const router = renderTable([INVESTIGATION]);
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
    renderTable([INVESTIGATION]);
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

  it("compares an investigation with the previous sweep of its own agent", async () => {
    const other = (investigation: InvestigationListItem) => ({
      ...investigation,
      observed_agent_key: "it_support_agent",
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
        trajectories_scanned: 1000,
        trajectories_eval_passed: passed,
        trajectories_eval_failed: 1000 - passed,
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
      const investigation = sweep(id, created, 0);
      return {
        ...investigation,
        counters: counters({
          ...investigation.counters,
          trajectories_scanned: scanned,
        }),
      };
    };
    renderTable([
      at("busy000001", "2026-09-14T06:00:20Z", 1000),
      at("quiet00001", "2026-09-14T03:00:20Z", 1),
      {
        ...INVESTIGATION,
        investigation_id: "empty00001",
        counters: counters(),
      },
    ]);
    await screen.findByRole("link", { name: "busy0000" });

    expect(
      cellUnder("Trajectories", "quiet000").querySelector("[style]"),
    ).toHaveStyle({ width: "4%" });
    expect(cellUnder("Trajectories", "empty000")).toHaveTextContent(/^—$/);
  });

  it("notes no window on a lone row, having nothing to set it apart from", async () => {
    renderTable([{ ...INVESTIGATION, window_start: "2026-09-13T00:00:00Z" }]);
    await screen.findByRole("link", { name: "abcdef12" });
    expect(screen.queryByText(/window$/)).toBeNull();
  });

  it("notes every window when no two rows share one", async () => {
    renderTable([
      INVESTIGATION,
      {
        ...INVESTIGATION,
        investigation_id: "bbbbbbbb99",
        window_start: "2026-09-13T00:00:00Z",
      },
      // A backfill, named by its bounds.
      {
        ...INVESTIGATION,
        investigation_id: "cccccccc99",
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
      INVESTIGATION,
      { ...INVESTIGATION, investigation_id: "bbbbbbbb99" },
      {
        ...INVESTIGATION,
        investigation_id: "nowindow99",
        window_start: null,
        window_end: null,
      },
    ]);
    await screen.findByRole("link", { name: "nowindow" });
    expect(
      within(cellUnder("Started", "nowindow")).queryByText("—"),
    ).toBeNull();
  });

  it("leaves a click on the investigation's own link to the link", async () => {
    // A modified click on the link opens its own tab; the row must not open a
    // second one.
    const open = vi.spyOn(window, "open").mockReturnValue(null);
    renderTable([INVESTIGATION]);
    fireEvent.click(await screen.findByRole("link", { name: "abcdef12" }), {
      ctrlKey: true,
    });
    expect(open).not.toHaveBeenCalled();
  });

  it("says on hover why a stalled investigation is called stalled", async () => {
    // Long past STALE_AFTER_MS, whatever today's date is.
    renderTable([{ ...INVESTIGATION, status: "running", finished_at: null }]);
    expect(await screen.findByText("Stalled")).toHaveAttribute(
      "title",
      expect.stringMatching(
        /^No update for \d+[mhd]\. The record still says running/,
      ),
    );
  });
});

describe("formatInvestigationOutcome and resolveInvestigationOutcomeTone", () => {
  const baseInvestigation: InvestigationListItem = {
    investigation_id: "test-investigation",
    observed_agent_key: "test-agent",
    created_at: new Date().toISOString(),
    finished_at: new Date().toISOString(),
    status: "done",
    elapsed_seconds: 10,
    metrics_passed: 0,
    metrics_failed: 0,
    metrics_errored: 0,
    error: null,
  };

  it("mutes a skipped investigation", () => {
    const investigation: InvestigationListItem = {
      ...baseInvestigation,
      status: "skipped",
    };
    expect(formatInvestigationOutcome(investigation)).toBe("skipped");
    expect(resolveInvestigationOutcomeTone(investigation)).toBe(
      "text-muted-foreground",
    );
  });

  it("writes a failed investigation in red", () => {
    const investigation: InvestigationListItem = {
      ...baseInvestigation,
      status: "failed",
    };
    expect(formatInvestigationOutcome(investigation)).toBe("failed");
    expect(resolveInvestigationOutcomeTone(investigation)).toBe(
      "text-red-600 dark:text-red-400",
    );
  });

  it("calls an investigation that carries an error failed, in red, whatever its status", () => {
    const investigation: InvestigationListItem = {
      ...baseInvestigation,
      status: "done",
      error: "Internal crash",
    };
    expect(formatInvestigationOutcome(investigation)).toBe("failed");
    expect(resolveInvestigationOutcomeTone(investigation)).toBe(
      "text-red-600 dark:text-red-400",
    );
  });

  it("mutes an investigation that failed for want of telemetry, and calls it no data", () => {
    const noDataErr =
      "400 Field name service_version does not exist in STRUCT<gen_ai_conversation_id STRING>";
    const investigation: InvestigationListItem = {
      ...baseInvestigation,
      status: "failed",
      error: noDataErr,
    };
    expect(formatInvestigationOutcome(investigation)).toBe("no data");
    expect(resolveInvestigationOutcomeTone(investigation)).toBe(
      "text-muted-foreground",
    );
  });

  it("calls an investigation nothing has updated in half an hour stalled, in amber", () => {
    // Created 2 hours ago with running status => stalled (> STALE_AFTER_MS = 30m)
    const staleTime = new Date(Date.now() - 2 * 3600 * 1000).toISOString();
    const investigation: InvestigationListItem = {
      ...baseInvestigation,
      status: "running",
      created_at: staleTime,
    };
    expect(formatInvestigationOutcome(investigation)).toBe("stalled");
    expect(resolveInvestigationOutcomeTone(investigation)).toBe(
      "text-amber-700 dark:text-amber-400",
    );
  });

  it("names an investigation still in flight by its status, muted", () => {
    const freshTime = new Date().toISOString();
    const runningInvestigation: InvestigationListItem = {
      ...baseInvestigation,
      status: "running",
      created_at: freshTime,
    };
    expect(formatInvestigationOutcome(runningInvestigation)).toBe("running");
    expect(resolveInvestigationOutcomeTone(runningInvestigation)).toBe(
      "text-muted-foreground",
    );

    const pendingInvestigation: InvestigationListItem = {
      ...baseInvestigation,
      status: "pending",
      created_at: freshTime,
    };
    expect(formatInvestigationOutcome(pendingInvestigation)).toBe("pending");
    expect(resolveInvestigationOutcomeTone(pendingInvestigation)).toBe(
      "text-muted-foreground",
    );
  });

  it("gives a finished investigation's pass rate as a percent", () => {
    const investigation: InvestigationListItem = {
      ...baseInvestigation,
      status: "done",
      counters: counters({
        trajectories_scanned: 10,
        trajectories_ingested: 10,
        trajectories_evaluated: 5,
        trajectories_eval_passed: 4,
        trajectories_eval_failed: 1,
      }),
    };
    expect(formatInvestigationOutcome(investigation)).toBe("80%");
    expect(resolveInvestigationOutcomeTone(investigation)).toBe(
      "text-muted-foreground",
    );
  });

  it("says a finished investigation that evaluated nothing evaluated nothing", () => {
    const investigation: InvestigationListItem = {
      ...baseInvestigation,
      status: "done",
      counters: counters(),
    };
    expect(formatInvestigationOutcome(investigation)).toBe(
      "no trajectories evaluated",
    );
    expect(resolveInvestigationOutcomeTone(investigation)).toBe(
      "text-muted-foreground",
    );
  });
});

describe("InvestigationsTable trigger badges", () => {
  it("badges the investigations a user started, and the sweeps not at all", async () => {
    renderTable([
      {
        ...INVESTIGATION,
        investigation_id: "custom0001",
        trigger_type: "custom",
      },
      {
        ...INVESTIGATION,
        investigation_id: "manual0001",
        trigger_type: "manual",
      },
      {
        ...INVESTIGATION,
        investigation_id: "sweep00001",
        trigger_type: "scheduled",
      },
    ]);
    await screen.findByRole("link", { name: "sweep000" });
    const rowOf = (id: string) =>
      screen.getByRole("link", { name: id.slice(0, 8) }).closest("tr")!;

    expect(within(rowOf("custom0001")).getByText("Custom")).toBeInTheDocument();
    expect(within(rowOf("manual0001")).getByText("Manual")).toBeInTheDocument();
    expect(within(rowOf("sweep00001")).queryByText("Custom")).toBeNull();
    expect(within(rowOf("sweep00001")).queryByText("Manual")).toBeNull();
    expect(screen.queryByTitle(/own schedule/)).toBeNull();
  });

  it("previews the focus a custom investigation was asked to review", async () => {
    renderTable([
      {
        ...INVESTIGATION,
        trigger_type: "custom",
        custom_overrides: {
          selector_sql: "SELECT 1",
          review_focus: "Refund requests the agent declined",
        },
      },
    ]);

    const focus = await screen.findByText("Refund requests the agent declined");
    expect(focus).toHaveAttribute(
      "title",
      "Refund requests the agent declined",
    );
    expect(screen.queryByText("SELECT 1")).toBeNull();
  });
});
