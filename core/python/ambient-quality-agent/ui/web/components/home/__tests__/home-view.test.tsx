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
 * Loading, empty and failed are three states.
 *
 * The first cut of this page rendered "Nothing found yet." for the four
 * seconds its lists spent in flight, on a deployment with four findings. That
 * is the confident-absence bug this codebase keeps shipping.
 */
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import {
  RouterProvider,
  createRootRoute,
  createRoute,
  createRouter,
  createMemoryHistory,
} from "@tanstack/react-router";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { HORIZON_LABEL } from "@/lib/day-buckets";
import { COUNTER_FIELDS } from "@/lib/counters";
import { STAGE_HELP } from "@/lib/funnel";
import { CHART_HELP, HomeView } from "../home-view";

function renderHome(fetchImpl: typeof fetch) {
  vi.stubGlobal("fetch", fetchImpl);
  const root = createRootRoute();
  const paths = [
    "/",
    "/c",
    "/insights",
    "/insights/$insightId",
    "/investigations",
    "/investigations/$runId",
  ];
  const [index, ...rest] = paths.map((path) =>
    createRoute({
      getParentRoute: () => root,
      path,
      ...(path === "/" ? { component: HomeView } : {}),
    }),
  );
  const router = createRouter({
    routeTree: root.addChildren([index, ...rest]),
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

const never = (() =>
  new Promise<Response>(() => {})) as unknown as typeof fetch;

const answers = (body: Record<string, unknown>) =>
  (async () =>
    new Response(JSON.stringify(body), { status: 200 })) as typeof fetch;

describe("HomeView", () => {
  beforeEach(() => vi.unstubAllGlobals());
  afterEach(() => vi.unstubAllGlobals());

  it("does not claim there is nothing while it is still loading", async () => {
    renderHome(never);

    // Give React a tick to paint the pending state.
    expect(await screen.findByText("Pipeline")).toBeInTheDocument();
    expect(screen.queryByText(/Nothing found yet/)).toBeNull();
    expect(screen.queryByText(/No trajectories evaluated yet/)).toBeNull();
    expect(screen.queryByText(/No investigations have run/)).toBeNull();
  });

  it("leads with the insights strip while the columns are stacked, and not beside them", async () => {
    renderHome(never);

    const latest = (
      await screen.findByRole("heading", { name: "Top insights" })
    ).closest("section");
    expect(latest).toHaveClass("order-first", "lg:order-none");
    // Only that column moves.
    const pipeline = screen
      .getByRole("heading", { name: "Pipeline" })
      .closest("section");
    expect(pipeline?.className).not.toMatch(/order-/);
  });

  it("offers top, latest and recurring insights, each the engine's own order", async () => {
    const asked: URLSearchParams[] = [];
    renderHome((async (input: RequestInfo | URL) => {
      const url = new URL(String(input), "http://x");
      if (url.pathname === "/api/insights") asked.push(url.searchParams);
      return new Response(JSON.stringify({ insights: [], total: 0 }), {
        status: 200,
      });
    }) as typeof fetch);
    const user = userEvent.setup();
    const strip = () =>
      asked.filter((p) => p.get("pageSize") === "5").map((p) => p.toString());

    const tabs = await screen.findByRole("group", { name: "Which insights" });
    expect(tabs).toHaveTextContent("TopLatestRecurring");
    await vi.waitFor(() =>
      expect(strip()).toContain("pageSize=5&orderBy=impact"),
    );

    await user.click(screen.getByRole("button", { name: "Latest" }));
    expect(
      screen.getByRole("heading", { name: "Latest insights" }),
    ).toBeInTheDocument();
    await vi.waitFor(() =>
      expect(strip()).toContain("pageSize=5&orderBy=recent"),
    );

    await user.click(screen.getByRole("button", { name: "Recurring" }));
    expect(screen.getByRole("button", { name: "Recurring" })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
    await vi.waitFor(() =>
      expect(strip()).toContain("status=RECURRING&pageSize=5&orderBy=impact"),
    );
    // The full list opens on the same filter.
    const section = screen
      .getByRole("heading", { name: "Recurring insights" })
      .closest("section")!;
    expect(section.querySelector('a[href^="/insights"]')).toHaveAttribute(
      "href",
      "/insights?status=recurring",
    );
  });

  it("says a section is empty once it knows that it is", async () => {
    renderHome(
      answers({ insights: [], total: 0, runs: [], days: [], stats: {} }),
    );

    expect(await screen.findByText(/Nothing found yet/)).toBeInTheDocument();
    expect(
      screen.getByText(/No trajectories evaluated yet/),
    ).toBeInTheDocument();
  });

  it("says which span the funnel's figures cover", async () => {
    // The funnel and the chart beneath it now describe the same fortnight.
    // They did not before — the funnel was all-time — and the page named
    // neither span, so the fix is half done if the caption stays silent.
    renderHome(
      answers({
        insights: [],
        total: 0,
        runs: [],
        days: [],
        stats: { investigations: 3 },
      }),
    );

    expect(
      await screen.findByText(`3 investigations, ${HORIZON_LABEL}`),
    ).toBeInTheDocument();
  });

  it("points each chart section at the page that lists what it counts", async () => {
    renderHome(
      answers({ insights: [], total: 0, runs: [], days: [], stats: {} }),
    );

    // The titles stay headings; the way to the page is a link beside them.
    for (const title of ["Pipeline", "Activity", "Insights"]) {
      expect(
        await screen.findByRole("heading", { name: title }),
      ).toBeInTheDocument();
      expect(
        screen.queryByRole("link", { name: new RegExp(`^${title}`) }),
      ).toBeNull();
    }
    const investigations = screen.getAllByRole("link", {
      name: "View investigations",
    });
    expect(investigations).toHaveLength(2);
    const insights = screen.getByRole("link", { name: "View insights" });
    for (const link of [...investigations, insights]) {
      expect(link).toHaveClass(
        "text-primary",
        "focus-visible:ring-2",
        "focus-visible:ring-inset",
      );
    }
    expect(investigations.map((a) => a.getAttribute("href"))).toEqual([
      "/investigations",
      "/investigations",
    ]);
    expect(insights).toHaveAttribute("href", "/insights");
  });

  it("focuses every header link the same way", async () => {
    renderHome(
      answers({ insights: [], total: 0, runs: [], days: [], stats: {} }),
    );

    // The sections clip overflow, so an outline drawn outside a link is cut off.
    const viewAll = await screen.findAllByRole("link", { name: "View all" });
    expect(viewAll).toHaveLength(2);
    for (const link of viewAll) {
      expect(link).toHaveClass(
        "focus-visible:ring-2",
        "focus-visible:ring-inset",
      );
    }
  });

  it("explains each chart behind a button beside its title", async () => {
    const user = userEvent.setup();
    renderHome(
      answers({ insights: [], total: 0, runs: [], days: [], stats: {} }),
    );

    await user.click(
      await screen.findByRole("button", { name: "About the Activity chart" }),
    );
    expect(
      await screen.findByRole("dialog", { hidden: true }),
    ).toHaveTextContent(/window ended that day/);
    await user.keyboard("{Escape}");

    await user.click(
      screen.getByRole("button", { name: "About the Insights chart" }),
    );
    expect(
      await screen.findByRole("dialog", { hidden: true }),
    ).toHaveTextContent(/unseen for the auto-resolve window/);
  });

  it("gives the auto-resolve window in days when the deployment sets one", async () => {
    const user = userEvent.setup();
    renderHome(
      answers({
        insights: [],
        total: 0,
        runs: [],
        days: [],
        stats: {},
        config: { insights_auto_resolve_days: 14 },
      }),
    );

    await user.click(
      await screen.findByRole("button", { name: "About the Insights chart" }),
    );
    expect(
      await screen.findByRole("dialog", { hidden: true }),
    ).toHaveTextContent(/unseen for 14 days/);
  });

  // Each series is bucketed by a different day: New by the creating
  // investigation's window_end, Resolved by when the sweep resolved it.
  it("says which day each chart counts a figure on", () => {
    expect(CHART_HELP.activity).toMatch(/window ended that day \(UTC\)/);
    expect(CHART_HELP.insightTrend(14)).toMatch(
      /created by the investigations whose window ended that day \(UTC\)/,
    );
    expect(CHART_HELP.insightTrend(14)).toMatch(/auto-resolved that day/);
  });

  // The hatched days are drawn from the legends' swatches, so the help uses
  // the legends' words.
  it("describes a hatched day in its legend's words", () => {
    expect(CHART_HELP.activity).toMatch(/Hatched: no trajectories evaluated\./);
    expect(CHART_HELP.insightTrend(null)).toMatch(
      /Hatched: no investigations\./,
    );
  });

  it("says the Activity chart draws no bar for eval service errors", () => {
    expect(CHART_HELP.activity).toMatch(/Eval service errors .*are not drawn/);
  });

  it("names every button on a populated Home differently", async () => {
    // The funnel's Insights stage and the Insights chart share a title, and
    // their (i) buttons open different texts.
    renderHome(
      answers({
        insights: [],
        total: 0,
        runs: [],
        days: [],
        stats: {
          traces_scanned: 20,
          traces_ingested: 10,
          traces_evaluated: 10,
          traces_eval_passed: 6,
          traces_eval_failed: 4,
          insights_created: 1,
          insights_recurring: 2,
        },
      }),
    );

    await screen.findByRole("img", { name: /^Insights: 3 total/ });
    const names = screen
      .getAllByRole("button")
      .map((b) => b.getAttribute("aria-label") ?? b.textContent);
    expect(names.filter((name, i) => names.indexOf(name) !== i)).toEqual([]);
    expect(names).toEqual(
      expect.arrayContaining([
        "About the Insights stage",
        "About the Insights chart",
      ]),
    );
  });

  const allHelp = () => [
    ...Object.values(STAGE_HELP),
    CHART_HELP.activity,
    CHART_HELP.insightTrend(14),
    CHART_HELP.insightTrend(null),
  ];

  it("writes no help text as an instruction to click", () => {
    for (const text of allHelp()) expect(text).not.toMatch(/click/i);
  });

  it("says created, not minted", () => {
    for (const text of allHelp()) expect(text).not.toMatch(/mint/i);
    for (const field of COUNTER_FIELDS) expect(field.help).not.toMatch(/mint/i);
  });

  it("links the funnel's stages from Home", async () => {
    renderHome(
      answers({
        insights: [],
        total: 0,
        runs: [],
        days: [],
        stats: {
          traces_evaluated: 4,
          traces_eval_passed: 4,
          insights_created: 1,
        },
      }),
    );

    expect(
      await screen.findByRole("link", { name: /^Insights\s*, view insights$/ }),
    ).toHaveAttribute("href", "/insights");
    expect(
      screen.getByRole("link", { name: /^Evaluated\s*, view investigations$/ }),
    ).toHaveAttribute("href", "/investigations");
  });

  it("reports a failed section instead of calling it empty", async () => {
    renderHome(
      (async () => new Response("nope", { status: 500 })) as typeof fetch,
    );

    expect(await screen.findAllByText(/Couldn't load/)).not.toHaveLength(0);
    expect(screen.queryByText(/Nothing found yet/)).toBeNull();
  });

  // The other half of the same rule: a read that failed because the deployment
  // has exported nothing is empty, not broken, and the panes say so in prose
  // rather than in red.
  it("treats a missing BigQuery export as no telemetry, not a broken page", async () => {
    renderHome((async () =>
      Response.json({
        error:
          "400 Field name service_version does not exist in STRUCT<" +
          "gen_ai_conversation_id STRING> at [28:24]; reason: invalidQuery",
      })) as typeof fetch);

    expect(
      await screen.findAllByText(/No telemetry to analyze yet/),
    ).not.toHaveLength(0);
    expect(screen.queryByText(/Couldn't load/)).toBeNull();
  });

  it("labels a failed sweep in Recent investigations as failed rather than no trajectories evaluated", async () => {
    renderHome(
      answers({
        insights: [],
        total: 0,
        days: [],
        stats: {},
        runs: [
          {
            run_id: "ede2106b",
            status: "failed",
            created_at: "2026-09-15T20:09:49Z",
            error: "BigQuery query timed out",
            counters: { traces_evaluated: 0, traces_eval_passed: 0 },
          },
        ],
      }),
    );

    expect(await screen.findByText("ede2106b")).toBeInTheDocument();
    expect(screen.getByText("Failed")).toBeInTheDocument();
    expect(
      within(screen.getByRole("table")).queryByText(
        "No trajectories evaluated",
      ),
    ).toBeNull();
  });

  it("lists the newest five investigations and counts those, not the page", async () => {
    const runs = Array.from({ length: 25 }, (_, i) => ({
      run_id: `run-${String(i).padStart(2, "0")}`,
      status: "done",
      created_at: new Date(Date.UTC(2026, 8, 1 + i, 8)).toISOString(),
      error: null,
      counters: {
        traces_eval_passed: 3,
        traces_eval_failed: 1,
        insights_created: i === 24 ? 2 : 0,
        insights_recurring: 5,
      },
    }));
    renderHome(answers({ insights: [], total: 0, days: [], stats: {}, runs }));

    expect(await screen.findByText("5 most recent")).toBeInTheDocument();
    expect(screen.queryByText("25 most recent")).toBeNull();
    // The newest five, newest first.
    const ids = screen.getAllByText(/^run-\d\d$/).map((el) => el.textContent);
    expect(ids).toHaveLength(5);
    expect(ids[0]).toBe("run-24");
    expect(ids).not.toContain("run-19");
    // Each row carries its outcome and the insights it recorded, as bare
    // counts under the New and Recurring headers.
    expect(screen.getAllByText("75%")).toHaveLength(5);
    const table = screen.getByRole("table");
    const column = (name: string) => {
      const index = within(table)
        .getAllByRole("columnheader")
        .findIndex((th) => th.textContent === name);
      return within(table)
        .getAllByRole("row")
        .slice(1)
        .map((row) => row.querySelectorAll("td")[index].textContent);
    };
    expect(column("New")).toEqual(["2", "0", "0", "0", "0"]);
    expect(column("Recurring")).toEqual(["5", "5", "5", "5", "5"]);
  });

  describe("Recent investigations rows", () => {
    const run = (over: Record<string, unknown>) => ({
      run_id: "run-00",
      observed_agent_name: "travel_desk_agent",
      status: "done",
      created_at: "2026-09-20T08:00:00Z",
      error: null,
      counters: { traces_eval_passed: 3, traces_eval_failed: 1 },
      ...over,
    });
    const home = (runs: unknown[]) =>
      renderHome(
        answers({ insights: [], total: 0, days: [], stats: {}, runs }),
      );
    const listedIds = () =>
      screen.getAllByText(/^(run|old|und)-\d\d$/).map((el) => el.textContent);

    it("dates and sorts a record without created_at by its window end", async () => {
      home([
        run({ run_id: "run-01", created_at: "2026-09-20T08:00:00Z" }),
        run({
          run_id: "old-01",
          created_at: undefined,
          window_end: "2026-09-22T08:00:00Z",
        }),
        run({ run_id: "und-01", created_at: undefined, window_end: null }),
      ]);

      expect(await screen.findByText("3 most recent")).toBeInTheDocument();
      // Newest first by whichever start time the record carries; undated last.
      expect(listedIds()).toEqual(["old-01", "run-01", "und-01"]);
      expect(screen.getAllByText("Undated")).toHaveLength(1);
    });

    it("names a run's state where it has no split to draw", async () => {
      home([
        run({
          run_id: "run-01",
          status: "running",
          // Recent: a run claiming to be in flight for days reads as stalled.
          created_at: new Date().toISOString(),
          counters: { traces_eval_passed: 2 },
        }),
        run({
          run_id: "run-02",
          error: "BigQuery query timed out",
          status: "failed",
        }),
        run({ run_id: "run-03", counters: {} }),
        run({ run_id: "run-04", counters: undefined }),
      ]);

      expect(await screen.findByText("Running")).toBeInTheDocument();
      expect(screen.getByText("Failed")).toHaveClass("text-red-600");
      expect(
        within(screen.getByRole("table")).getAllByText(
          "No trajectories evaluated",
        ),
      ).toHaveLength(2);
      expect(screen.queryByText(/^\d+\/\d+$/)).toBeNull();
    });

    it("draws a run whose every evaluation errored rather than hiding it", async () => {
      home([run({ counters: { traces_eval_errored: 5 } })]);

      // No rate to give, since nothing was judged; the errors are counted instead.
      const link = await screen.findByRole("link", { name: "run-00" });
      const headers = within(screen.getByRole("table"))
        .getAllByRole("columnheader")
        .map((th) => th.textContent);
      const cells = link.closest("tr")!.querySelectorAll("td");
      expect(cells[headers.indexOf("Pass rate")]).toHaveTextContent(/^—$/);
      expect(cells[headers.indexOf("Errors")]).toHaveTextContent(/^5$/);
    });

    it("compares its last row with the run before it, which it does not show", async () => {
      home([
        ...[0, 1, 2, 3, 4].map((i) =>
          run({
            run_id: `run-0${i}`,
            created_at: `2026-09-20T0${8 - i}:00:00Z`,
          }),
        ),
        // The sixth, off the list: 25% before the fifth row's 75%.
        run({
          run_id: "old-01",
          created_at: "2026-09-20T02:00:00Z",
          counters: { traces_eval_passed: 1, traces_eval_failed: 3 },
        }),
      ]);

      const link = await screen.findByRole("link", { name: "run-04" });
      expect(link.closest("tr")).toHaveTextContent(/up 50%/);
      expect(screen.queryByRole("link", { name: "old-01" })).toBeNull();
    });

    it("gives an empty list no count", async () => {
      home([]);

      expect(
        await screen.findAllByText(/No investigations have run/),
      ).not.toHaveLength(0);
      expect(screen.queryByText(/most recent/)).toBeNull();
    });
  });
});
