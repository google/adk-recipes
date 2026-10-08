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
 * A deployment that has exported no telemetry is empty, not broken.
 *
 * b/563290003: the first thing a new customer saw was a column of `⚠ error`
 * and a red box holding a BigQuery 400, on a project whose only fault was not
 * having served any traffic yet. These pin the two readings apart on both
 * investigation surfaces -- a genuine fault stays red, because the point is to
 * tell them apart rather than to mute everything.
 */
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import {
  createMemoryHistory,
  createRootRoute,
  createRoute,
  createRouter,
  RouterProvider,
} from "@tanstack/react-router";
import { render, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { InvestigationDetailView } from "../investigation-detail-view";
import { InvestigationsView } from "../investigations-view";

/** The failure b/563290003 was filed on: BigQuery infers the sink's `labels`
 *  STRUCT from the rows it holds, so a column no exported trace has carried
 *  does not exist and the sweep's query is a 400. */
const NO_TELEMETRY =
  "400 Field name service_version does not exist in STRUCT<" +
  "gen_ai_conversation_id STRING, gen_ai_agent_name STRING> at [28:24]; " +
  "reason: invalidQuery";

const RUN = {
  run_id: "b9e3d6d711",
  observed_agent_name: "it_support_agent",
  created_at: "2026-09-18T14:29:30Z",
  finished_at: "2026-09-18T14:30:43Z",
  status: "failed",
  elapsed_seconds: 73.5,
  metrics_passed: 0,
  metrics_failed: 0,
  metrics_errored: 0,
  error: NO_TELEMETRY,
};

function serveList(error: string) {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string) => {
      const body = url.includes("/api/investigations")
        ? { runs: [{ ...RUN, error }] }
        : url.includes("/api/stats")
          ? { stats: {} }
          : { days: [] };
      return Response.json(body);
    }),
  );
  renderRoute(<InvestigationsView />, "/investigations");
}

function serveDetail(error: string) {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string) =>
      url.includes("/api/insights")
        ? Response.json({ insights: [], total: 0 })
        : Response.json({ run: { ...RUN, error, summary: {}, events: [] } }),
    ),
  );
  renderRoute(<InvestigationDetailView runId={RUN.run_id} />, "/");
}

/** What the one row says under Pass rate, where a run with no count to show
 *  names its state. */
async function stateCell(): Promise<HTMLElement> {
  const link = await screen.findByRole("link", { name: "b9e3d6d7" });
  const table = link.closest("table")!;
  const index = [...table.querySelectorAll("thead th")].findIndex(
    (th) => th.textContent === "Pass rate",
  );
  return within(link.closest("tr")!).getAllByRole("cell")[index];
}

/** Both views link to `/investigations/$runId`, so the route has to exist. */
function renderRoute(element: React.ReactElement, at: string) {
  const root = createRootRoute();
  const children = [at, "/investigations/$runId"]
    .filter((path, i, all) => all.indexOf(path) === i)
    .map((path) =>
      createRoute({
        getParentRoute: () => root,
        path,
        ...(path === at ? { component: () => element } : {}),
      }),
    );
  const router = createRouter({
    routeTree: root.addChildren(children),
    history: createMemoryHistory({ initialEntries: [at] }),
  });
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: 0 } },
  });
  render(
    <QueryClientProvider client={qc}>
      {/* biome-ignore lint/suspicious/noExplicitAny: the test router is not the app's registered router type. */}
      <RouterProvider router={router as any} />
    </QueryClientProvider>,
  );
}

describe("a sweep that found no telemetry", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("is flagged as no data on the runs table, not as a failure", async () => {
    serveList(NO_TELEMETRY);

    const status = await stateCell();
    expect(status).toHaveTextContent("No data");
    expect(status).not.toHaveTextContent("Failed");
    // Muted, so a page of them does not read as a broken deployment, and
    // explained on hover rather than as a BigQuery 400.
    expect(status.firstElementChild).toHaveClass("text-muted-foreground");
    expect(status.firstElementChild).toHaveAttribute(
      "title",
      expect.stringContaining('"service_version"'),
    );
    expect(status.firstElementChild).not.toHaveAttribute(
      "title",
      expect.stringContaining("invalidQuery"),
    );
  });

  it("still flags a genuine fault as failed, with the error on hover", async () => {
    serveList("PermissionDenied: 403 Access Denied: Table aqua.spans");

    const status = await stateCell();
    expect(status).toHaveTextContent("Failed");
    expect(status.firstElementChild).toHaveClass("text-red-600");
    expect(status.firstElementChild).toHaveAttribute(
      "title",
      "PermissionDenied: 403 Access Denied: Table aqua.spans",
    );
  });

  it("explains itself on the detail page instead of showing a 400", async () => {
    serveDetail(NO_TELEMETRY);

    expect(
      await screen.findByText(/No telemetry to analyze yet/),
    ).toBeInTheDocument();
    // Named in the hint, so whoever set the sink up knows which signal never
    // arrived without opening the detail -- which also names it.
    expect(
      screen.getByText(
        /The exported trajectories carry no "service_version" field/,
      ),
    ).toBeInTheDocument();
    // Not lost, just not the first thing the page says.
    expect(screen.getByText("Details")).toBeInTheDocument();
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("still raises a genuine fault on the detail page", async () => {
    serveDetail("PermissionDenied: 403 Access Denied: Table aqua.spans");

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("This investigation stopped early");
    expect(alert).toHaveTextContent("403 Access Denied");
  });
});
