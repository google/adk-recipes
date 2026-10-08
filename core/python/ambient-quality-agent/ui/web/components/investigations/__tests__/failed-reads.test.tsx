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
 * Loading, empty and failed are three states, and each of the view's three
 * reads renders them independently: a failed read names itself and leaves the
 * other sections alone.
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

const RUN = {
  run_id: "abcdef1234",
  observed_agent_name: "travel_desk_agent",
  created_at: "2026-09-14T06:00:00Z",
  finished_at: "2026-09-14T06:05:00Z",
  status: "done",
  rca_status: "",
  elapsed_seconds: 12,
  metrics_passed: 3,
  metrics_failed: 1,
  metrics_errored: 0,
  error: null,
};

/** Serves each route, or fails the ones named in `broken` with a 500. */
function mount(broken: string[] = [], runs: unknown[] = [RUN]) {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string) => {
      const path = ["/api/investigations", "/api/stats", "/api/daily"].find(
        (p) => url.includes(p),
      );
      if (path && broken.includes(path)) {
        return new Response("upstream exploded", { status: 500 });
      }
      const body =
        path === "/api/investigations"
          ? { runs }
          : path === "/api/stats"
            ? { stats: { investigations: 1, traces_scanned: 10 } }
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

describe("InvestigationsView when a read fails", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("calls a failed investigations read a failure, not an empty list", async () => {
    mount(["/api/investigations"]);

    expect(
      await screen.findByText(/Couldn't load the investigations/),
    ).toBeInTheDocument();
    expect(screen.queryByText("No investigations yet.")).toBeNull();
  });

  it("still says the list is empty when the read succeeded and returned nothing", async () => {
    mount([], []);

    expect(
      await screen.findByText("No investigations yet."),
    ).toBeInTheDocument();
    expect(screen.queryByText(/Couldn't load/)).toBeNull();
  });

  it("keeps the runs table when only the totals fail", async () => {
    mount(["/api/stats"]);

    expect(
      await screen.findByText(/Couldn't load the totals/),
    ).toBeInTheDocument();
    // The row is still there: the totals and the table are separate reads.
    expect(
      await screen.findByRole("link", { name: "abcdef12" }),
    ).toBeInTheDocument();
    expect(screen.queryByText(/Couldn't load the investigations/)).toBeNull();
  });

  it("fails the daily counts on their own", async () => {
    mount(["/api/daily"]);

    expect(
      await screen.findByText(/Couldn't load the daily counts/),
    ).toBeInTheDocument();
    expect(
      await screen.findByRole("link", { name: "abcdef12" }),
    ).toBeInTheDocument();
  });

  it("names each failed section separately rather than breaking the page", async () => {
    mount(["/api/investigations", "/api/stats", "/api/daily"]);

    const alerts = await screen.findAllByRole("alert");
    expect(alerts.map((a) => a.textContent)).toEqual([
      expect.stringContaining("the totals"),
      expect.stringContaining("the daily counts"),
      expect.stringContaining("the investigations"),
    ]);
  });
});
