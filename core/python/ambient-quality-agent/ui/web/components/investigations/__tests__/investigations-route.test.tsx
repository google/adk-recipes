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
 * The `/investigations` page keeps "Only with failures" in the URL.
 *
 * Mounted through the real route: the URL a link or a reload arrives with,
 * the checkbox that reads it, and the URL a click on it leaves behind.
 */
import { afterEach, describe, expect, it, vi } from "vitest";
import type { ReactNode } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import {
  createMemoryHistory,
  createRootRoute,
  createRoute,
  createRouter,
  RouterProvider,
} from "@tanstack/react-router";
import { Route as InvestigationsRoute } from "@/src/routes/investigations.index";
import { RUNS_LIST_LIMIT } from "@/lib/aqua-api";

// The sidebar reads chat sessions over A2A, which is not what is under test.
vi.mock("@/components/nav/page-shell", () => ({
  PageShell: ({ children }: { children: ReactNode }) => <>{children}</>,
}));

const run = (runId: string, failed: number) => ({
  run_id: runId,
  observed_agent_name: "travel_desk_agent",
  created_at: "2026-09-14T06:00:00Z",
  status: "done",
  error: null,
  counters: {
    traces_evaluated: 4,
    traces_eval_passed: 4 - failed,
    traces_eval_failed: failed,
  },
});

function mountPage(
  at: string,
  {
    runs = [run("cleaaaaa11", 0), run("failfail22", 1)],
    days = [] as Record<string, unknown>[],
    // What a read for one day returns, when it differs from the whole list.
    dayRuns = undefined as Record<string, unknown>[] | undefined,
  } = {},
) {
  const urls: string[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string) => {
      urls.push(url);
      const body = url.includes("/api/investigations")
        ? { runs: dayRuns && url.includes("windowStart=") ? dayRuns : runs }
        : url.includes("/api/stats")
          ? { stats: {} }
          : { days };
      return new Response(JSON.stringify(body), { status: 200 });
    }),
  );
  const root = createRootRoute();
  const page = InvestigationsRoute.update({
    id: "/investigations/",
    path: "/investigations/",
    getParentRoute: () => root,
    // biome-ignore lint/suspicious/noExplicitAny: the test route tree is not the app's registered route tree.
  } as any);
  const detail = createRoute({
    getParentRoute: () => root,
    path: "/investigations/$runId",
  });
  const router = createRouter({
    routeTree: root.addChildren([page, detail]),
    history: createMemoryHistory({ initialEntries: [at] }),
  });
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={qc}>
      {/* biome-ignore lint/suspicious/noExplicitAny: the test router is not the app's registered router type. */}
      <RouterProvider router={router as any} />
    </QueryClientProvider>,
  );
  return Object.assign(router, { urls });
}

const validate = InvestigationsRoute.options.validateSearch as (
  search: Record<string, unknown>,
) => Record<string, unknown>;

const checkbox = () =>
  screen.getByRole("checkbox", { name: /only with failures/i });

describe("the /investigations failures param", () => {
  it("keeps failures=only", () => {
    expect(validate({ failures: "only" }).failures).toBe("only");
  });

  it("drops any other value, which reads as the whole list", () => {
    for (const failures of ["all", "yes", "ONLY", "", 1, true]) {
      expect(validate({ failures }).failures).toBeUndefined();
    }
    expect(validate({})).toEqual({ failures: undefined, day: undefined });
  });

  it("keeps a real day and drops anything else", () => {
    expect(validate({ day: "2026-09-20" }).day).toBe("2026-09-20");
    for (const day of ["2026-02-30", "foo", 20260920]) {
      expect(validate({ day }).day).toBeUndefined();
    }
  });
});

describe("the /investigations page", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("opens filtered when the URL says failures=only", async () => {
    mountPage("/investigations?failures=only");

    await screen.findByRole("link", { name: "failfail" });
    expect(checkbox()).toBeChecked();
    expect(screen.queryByRole("link", { name: "cleaaaaa" })).toBeNull();
  });

  it("writes the checkbox into the URL, and unchecked as no param", async () => {
    const router = mountPage("/investigations");
    await screen.findByRole("link", { name: "cleaaaaa" });
    const entries = router.history.length;

    await userEvent.click(checkbox());
    await waitFor(() =>
      expect(router.state.location.search).toEqual({ failures: "only" }),
    );
    expect(screen.queryByRole("link", { name: "cleaaaaa" })).toBeNull();

    await userEvent.click(checkbox());
    await waitFor(() => expect(router.state.location.searchStr).toBe(""));
    expect(screen.getByRole("link", { name: "cleaaaaa" })).toBeInTheDocument();
    expect(router.history.length).toBe(entries);
  });

  it("unchecks when a link drops the param under a mounted page", async () => {
    // The sidebar's "All investigations" link while the filter is on.
    const router = mountPage("/investigations?failures=only");
    await screen.findByRole("link", { name: "failfail" });

    await act(() => router.navigate({ to: "/investigations" }));

    await waitFor(() => expect(checkbox()).not.toBeChecked());
    expect(screen.getByRole("link", { name: "cleaaaaa" })).toBeInTheDocument();
  });
});

describe("the /investigations page narrowed to a day", () => {
  afterEach(() => vi.unstubAllGlobals());

  const DAY_READ =
    "/api/investigations?windowStart=2026-09-20T00%3A00%3A00.000Z&windowEnd=2026-09-20T23%3A59%3A59.999999Z";

  it("asks for the runs whose window closed that UTC day, as the chart buckets them", async () => {
    const router = mountPage("/investigations?day=2026-09-20&failures=only");

    await screen.findByRole("link", { name: "failfail" });
    expect(router.urls).toContain(DAY_READ);
    expect(checkbox()).toBeChecked();
    // The chip beside the heading shows the day; the heading says it only to a
    // screen reader, which reaches it by heading navigation without the chip.
    const heading = screen.getByRole("heading", {
      name: "Investigations · 2026-09-20 (UTC)",
    });
    expect(within(heading).getByText(/2026-09-20/)).toHaveClass("sr-only");
    expect(
      screen.getByRole("button", { name: /^Day 2026-09-20 \(UTC\)/ }),
    ).toBeInTheDocument();
  });

  it("compares a day's first run with the one before it, outside the day", async () => {
    const sweep = (runId: string, created: string, passed: number) => ({
      ...run(runId, 4 - passed),
      created_at: created,
    });
    const first = sweep("dayfirst01", "2026-09-20T02:00:00Z", 3);
    mountPage("/investigations?day=2026-09-20", {
      dayRuns: [first],
      runs: [first, sweep("daybefore1", "2026-09-19T20:00:00Z", 2)],
    });

    // 75% after 50%, though the read for the day holds only the first run.
    const link = await screen.findByRole("link", { name: "dayfirst" });
    await waitFor(() => expect(link.closest("tr")).toHaveTextContent(/up 25%/));
  });

  it("says the day was empty rather than the deployment", async () => {
    mountPage("/investigations?day=2026-09-20", { runs: [] });

    expect(
      await screen.findByText("No investigations on 2026-09-20 (UTC)."),
    ).toBeInTheDocument();
  });

  it("says when the day holds more runs than one read returns", async () => {
    mountPage("/investigations?day=2026-09-20", {
      days: [{ day: "2026-09-20", investigations: 60 }],
    });

    expect(
      await screen.findByText(
        /Showing the newest 2 of 60 investigations on 2026-09-20/,
      ),
    ).toBeInTheDocument();
  });

  it("does not claim a cut when the read holds the whole day", async () => {
    mountPage("/investigations?day=2026-09-20", {
      days: [{ day: "2026-09-20", investigations: 2 }],
    });

    await screen.findByRole("link", { name: "failfail" });
    expect(screen.queryByText(/Showing the newest/)).toBeNull();
  });

  it("does not claim a cut for a short day outside the chart's span", async () => {
    mountPage("/investigations?day=2026-09-20", {
      days: [{ day: "2026-09-21", investigations: 60 }],
    });

    await screen.findByRole("link", { name: "failfail" });
    expect(screen.queryByText(/Showing the newest/)).toBeNull();
  });

  it("warns of a cut when a day outside the chart's span fills the read", async () => {
    const runs = Array.from({ length: RUNS_LIST_LIMIT }, (_, i) =>
      run(`run${String(i).padStart(4, "0")}xx`, 0),
    );
    mountPage("/investigations?day=2026-09-20", { runs });

    expect(
      await screen.findByText(
        `Showing the newest ${RUNS_LIST_LIMIT} investigations on 2026-09-20 (UTC); the day may hold more.`,
      ),
    ).toBeInTheDocument();
  });

  it("says the day's runs all passed, not every run", async () => {
    mountPage("/investigations?day=2026-09-20&failures=only", {
      runs: [run("cleaaaaa11", 0)],
    });

    expect(
      await screen.findByText(
        /^All investigations on 2026-09-20 \(UTC\) passed every metric\./,
      ),
    ).toBeInTheDocument();
  });

  it("clears the day with one button, keeps the checkbox, and hands it focus", async () => {
    const router = mountPage("/investigations?day=2026-09-20&failures=only");
    await screen.findByRole("link", { name: "failfail" });

    await userEvent.click(
      screen.getByRole("button", {
        name: "Day 2026-09-20 (UTC), remove filter",
      }),
    );

    await waitFor(() =>
      expect(router.state.location.search).toEqual({ failures: "only" }),
    );
    expect(router.urls).toContain("/api/investigations");
    expect(document.activeElement).toBe(checkbox());
  });
});
