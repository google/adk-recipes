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
 * The rail shows the ten newest runs of whatever the server returned, newest
 * first, under a header linking to the full list. Each row names the run's
 * state when it has no outcome to show; loading, empty and failed are three
 * different lines, and a failed refetch keeps the rows already on screen.
 */
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import {
  RouterProvider,
  createMemoryHistory,
  createRootRoute,
  createRoute,
  createRouter,
} from "@tanstack/react-router";
import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { resetRailScrollForTest } from "@/components/chat/use-rail-scroll";
import { RecentRunsRail } from "../recent-runs-rail";

/** Twelve runs, one per hour, oldest first in the payload. */
const RUNS = Array.from({ length: 12 }, (_, i) => ({
  run_id: `run${String(i).padStart(2, "0")}`,
  created_at: `2026-09-14T${String(i).padStart(2, "0")}:00:00Z`,
  status: "done",
  counters: { traces_eval_passed: 90, traces_eval_failed: 10 },
  error: null,
}));

const ok = (runs: unknown[]) =>
  vi.fn(async () => new Response(JSON.stringify({ runs }), { status: 200 }));

function renderRail(
  fetchImpl: ReturnType<typeof vi.fn>,
  { path = "/", client }: { path?: string; client?: QueryClient } = {},
) {
  vi.stubGlobal("fetch", fetchImpl);
  const root = createRootRoute({ component: RecentRunsRail });
  const children = ["/", "/investigations", "/investigations/$runId"].map((p) =>
    createRoute({ getParentRoute: () => root, path: p }),
  );
  const router = createRouter({
    routeTree: root.addChildren(children),
    history: createMemoryHistory({ initialEntries: [path] }),
  });
  const queryClient =
    client ??
    new QueryClient({
      defaultOptions: { queries: { retry: false, gcTime: 0 } },
    });
  render(
    <QueryClientProvider client={queryClient}>
      <RouterProvider router={router} />
    </QueryClientProvider>,
  );
  return queryClient;
}

const runRows = () =>
  screen
    .getAllByRole("link")
    .filter((a) => /\/investigations\/\w+$/.test(a.getAttribute("href") ?? ""));

beforeEach(() => resetRailScrollForTest());
afterEach(() => vi.unstubAllGlobals());

describe("RecentRunsRail", () => {
  it("shows the ten newest runs, newest first, under a link to all of them", async () => {
    renderRail(ok(RUNS));

    await screen.findByRole("link", { name: "All investigations" });
    const rows = await waitFor(() => {
      const r = runRows();
      expect(r).toHaveLength(10);
      return r;
    });
    expect(rows[0]).toHaveAttribute("href", "/investigations/run11");
    expect(rows[9]).toHaveAttribute("href", "/investigations/run02");
    expect(rows[0]).toHaveTextContent("90%");
  });

  it("says what a row's ratio counts, to screen readers and on hover", async () => {
    renderRail(ok(RUNS));

    const [row] = await screen.findAllByRole("link", {
      name: /90 passed, 10 failed/,
    });
    // The pass rate the runs table shows for the same run.
    expect(within(row).getByTitle("90 passed, 10 failed")).toHaveTextContent(
      "90%",
    );
  });

  it("leaves eval-service errors out of the pass rate, as the runs table does", async () => {
    renderRail(
      ok([
        {
          ...RUNS[0],
          counters: {
            traces_eval_passed: 30,
            traces_eval_failed: 10,
            traces_eval_errored: 5,
          },
        },
      ]),
    );

    const [row] = await screen.findAllByRole("link", {
      name: /30 passed, 10 failed/,
    });
    expect(row).toHaveTextContent("75%");
  });

  it("has no count in its header: the server returns only the newest runs", async () => {
    renderRail(ok(RUNS));

    await screen.findByRole("link", { name: "All investigations" });
    expect(
      screen.getByRole("heading", { level: 2, name: "Recent investigations" }),
    ).toBeInTheDocument();
  });

  it("says so when there are no runs", async () => {
    renderRail(ok([]));

    expect(
      await screen.findByText("No investigations yet."),
    ).toBeInTheDocument();
    expect(runRows()).toHaveLength(0);
  });

  it("takes no share of the rail for its empty line", async () => {
    renderRail(ok([]));
    await screen.findByText("No investigations yet.");

    expect(
      screen.getByRole("region", { name: "Recent investigations" }).className,
    ).not.toContain("flex-1");
  });

  it("takes no share of the rail for its error line", async () => {
    renderRail(vi.fn(async () => new Response("{}", { status: 500 })));
    await screen.findByRole("button", { name: "Retry" });

    expect(
      screen.getByRole("region", { name: "Recent investigations" }).className,
    ).not.toContain("flex-1");
  });

  it("survives a run with no created_at, listing it last", async () => {
    renderRail(
      ok([
        { run_id: "undated", created_at: null, error: null },
        ...RUNS.slice(0, 2),
      ]),
    );

    const undated = await screen.findByRole("link", { name: /Undated/ });
    expect(runRows().at(-1)).toBe(undated);
  });

  it.each([
    [{ status: "pending", counters: {} }, "Pending"],
    [{ status: "running", counters: {} }, "Running"],
    [
      { status: "running", counters: {}, created_at: "2020-01-01T00:00:00Z" },
      "Stalled",
    ],
    [{ status: "skipped", counters: {} }, "Skipped"],
    [{ status: "failed", counters: {} }, "Failed"],
    [{ status: "failed", counters: {}, error: "boom" }, "Failed"],
    [
      {
        status: "failed",
        counters: {},
        error: "Not found: Table p:d.t was not found",
      },
      "No data",
    ],
    [{ status: "done", counters: {} }, "No data"],
  ])(
    "names a run's state when it has no outcome: %j -> %s",
    async (patch, word) => {
      renderRail(
        ok([{ ...RUNS[0], created_at: new Date().toISOString(), ...patch }]),
      );

      const [row] = await waitFor(() => {
        const r = runRows();
        expect(r).toHaveLength(1);
        return r;
      });
      expect(within(row).getByText(word)).toBeInTheDocument();
    },
  );

  it("badges an investigation that evaluated nothing as no data, with the full phrase on hover", async () => {
    renderRail(
      ok([
        {
          ...RUNS[0],
          created_at: new Date().toISOString(),
          status: "done",
          counters: {},
        },
      ]),
    );

    expect(
      await screen.findByTitle("No trajectories evaluated"),
    ).toHaveTextContent("No data");
  });

  it("reserves its rows with a skeleton while loading", async () => {
    renderRail(vi.fn(() => new Promise<Response>(() => {})));

    expect(
      await screen.findByRole("status", { name: /loading investigations/i }),
    ).toBeInTheDocument();
    expect(
      screen.queryByText("No investigations yet."),
    ).not.toBeInTheDocument();
  });

  it("offers a retry when loading failed with nothing cached", async () => {
    const fetchMock = vi.fn(async () => new Response("{}", { status: 500 }));
    renderRail(fetchMock);

    await userEvent.click(await screen.findByRole("button", { name: "Retry" }));

    expect(screen.getByText(/Couldn.t load/)).toBeInTheDocument();
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2));
  });

  it("keeps the cached rows when a refetch fails", async () => {
    const client = renderRail(ok(RUNS));
    await waitFor(() => expect(runRows()).toHaveLength(10));

    vi.stubGlobal(
      "fetch",
      vi.fn(async () => new Response("{}", { status: 500 })),
    );
    await act(async () => {
      await client.refetchQueries({ queryKey: ["aqua", "runs"] });
      // react-query notifies observers on a later tick.
      await new Promise((resolve) => setTimeout(resolve, 10));
    });

    expect(client.getQueryState(["aqua", "runs"])?.status).toBe("error");
    expect(runRows()).toHaveLength(10);
    expect(
      screen.queryByRole("button", { name: "Retry" }),
    ).not.toBeInTheDocument();
  });

  it("marks the current route's run", async () => {
    renderRail(ok(RUNS), { path: "/investigations/run05" });

    await waitFor(() => expect(runRows()).toHaveLength(10));
    const active = runRows().find(
      (a) => a.getAttribute("aria-current") === "page",
    );
    expect(active).toHaveAttribute("href", "/investigations/run05");
    expect(active?.className).toContain("bg-accent");
  });
});
