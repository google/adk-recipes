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
 * The list asks the engine for one page at a time.
 *
 * Every page is a read: it requests the rows it draws, in the order it draws
 * them, and Next fetches the next page rather than slicing one already in
 * hand. What the requests carry is as much the subject here as what renders —
 * a list that pages correctly over a first page it asked wrongly for is the
 * bug this replaced.
 */
import { describe, expect, it, vi, beforeEach } from "vitest";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import {
  createMemoryHistory,
  createRootRoute,
  createRoute,
  createRouter,
  RouterProvider,
} from "@tanstack/react-router";
import { INSIGHTS_PAGE_SIZE, type InsightView } from "@/lib/aqua-api";
import { InsightsView } from "../insights-view";

const insight = (n: number): InsightView => ({
  insight_id: `ins-${n}`,
  agent_name: "it_support_agent",
  label: `issue ${n}`,
  tool_name: "create_ticket",
  status: "RECURRING",
  occurrence_count: 1,
  trace_count: n,
  impact: n,
  last_run_at: "2026-08-28T18:35:34Z",
  // No diagnosis, so the card's headline is the label these assertions name.
  diagnosis: "",
  confidence: 1,
});

/** A page of `count` issues numbered from `first`, out of `total`. */
function page(
  first: number,
  count: number,
  total: number,
  nextToken: string | null,
) {
  return JSON.stringify({
    insights: Array.from({ length: count }, (_, i) => insight(first + i)),
    total,
    next_page_token: nextToken,
  });
}

/** Renders the list against a fetch of the caller's, recording the URLs it asks for. */
function renderList(fetchImpl: typeof fetch): { urls: string[] } {
  const urls: string[] = [];
  vi.stubGlobal("fetch", ((input: RequestInfo | URL, init?: RequestInit) => {
    urls.push(typeof input === "string" ? input : input.toString());
    return fetchImpl(input, init);
  }) as typeof fetch);
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: 0 } },
  });
  const rootRoute = createRootRoute();
  const indexRoute = createRoute({
    getParentRoute: () => rootRoute,
    path: "/",
    component: () => <InsightsView />,
  });
  const detailRoute = createRoute({
    getParentRoute: () => rootRoute,
    path: "/insights/$insightId",
    component: () => <div>Detail</div>,
  });
  const router = createRouter({
    routeTree: rootRoute.addChildren([indexRoute, detailRoute]),
    history: createMemoryHistory({ initialEntries: ["/"] }),
  });
  render(
    <QueryClientProvider client={queryClient}>
      <RouterProvider router={router} />
    </QueryClientProvider>,
  );
  return { urls };
}

/** Serves one fixed page, whatever is asked for. */
function serving(body: string): typeof fetch {
  return (() =>
    Promise.resolve(new Response(body, { status: 200 }))) as typeof fetch;
}

/** The list reads: what it asked `/api/insights` for, in order. */
const listReads = (urls: string[]) =>
  urls.filter((u) => u.startsWith("/api/insights?") || u === "/api/insights");

describe("the insights list's paging", () => {
  beforeEach(() => vi.restoreAllMocks());

  it("asks for the rows it draws, ranked by the engine", async () => {
    const { urls } = renderList(serving(page(1, INSIGHTS_PAGE_SIZE, 87, "t2")));

    await screen.findByText("issue 1");
    const first = listReads(urls)[0];
    expect(first).toContain(`pageSize=${INSIGHTS_PAGE_SIZE}`);
    expect(first).toContain("orderBy=impact");
    expect(first).not.toContain("pageToken");
  });

  it("counts the pages off the engine's total, not the rows in hand", async () => {
    renderList(serving(page(1, INSIGHTS_PAGE_SIZE, 87, "t2")));

    // 87 matches at ten a page, and this is the first of them.
    expect(
      await screen.findByText(/Page 1 of 9 \(10 of 87\)/),
    ).toBeInTheDocument();
  });

  it("fetches the next page instead of slicing the one it has", async () => {
    const { urls } = renderList((async (input: RequestInfo | URL) => {
      const url = typeof input === "string" ? input : input.toString();
      return new Response(
        url.includes("pageToken=t2")
          ? page(11, INSIGHTS_PAGE_SIZE, 87, "t3")
          : page(1, INSIGHTS_PAGE_SIZE, 87, "t2"),
        { status: 200 },
      );
    }) as typeof fetch);

    await screen.findByText("issue 1");
    await userEvent.click(screen.getByRole("button", { name: /Next/i }));

    expect(await screen.findByText("issue 11")).toBeInTheDocument();
    expect(screen.queryByText("issue 1")).toBeNull();
    expect(screen.getByText(/Page 2 of 9/)).toBeInTheDocument();
    expect(listReads(urls).some((u) => u.includes("pageToken=t2"))).toBe(true);
  });

  it("walks back to the page it came from", async () => {
    renderList((async (input: RequestInfo | URL) => {
      const url = typeof input === "string" ? input : input.toString();
      return new Response(
        url.includes("pageToken=t2")
          ? page(11, INSIGHTS_PAGE_SIZE, 87, "t3")
          : page(1, INSIGHTS_PAGE_SIZE, 87, "t2"),
        { status: 200 },
      );
    }) as typeof fetch);

    await screen.findByText("issue 1");
    await userEvent.click(screen.getByRole("button", { name: /Next/i }));
    await screen.findByText("issue 11");
    await userEvent.click(screen.getByRole("button", { name: /Previous/i }));

    expect(await screen.findByText(/Page 1 of 9/)).toBeInTheDocument();
    expect(screen.getByText("issue 1")).toBeInTheDocument();
  });

  it("stops at the last page, which is where the engine stops offering one", async () => {
    // A page that fills itself but is the final one: only the absent token
    // says so, and a pager counting rows would offer a tenth page of nothing.
    renderList(serving(page(1, INSIGHTS_PAGE_SIZE, INSIGHTS_PAGE_SIZE, null)));

    await screen.findByText("issue 1");
    expect(screen.queryByRole("button", { name: /Next/i })).toBeNull();
  });

  it("offers no page after a full one that divides the total exactly", async () => {
    // Thirty issues at ten a page: the third page is full, and the boundary
    // where an off-by-one would show up as an empty fourth.
    const total = 3 * INSIGHTS_PAGE_SIZE;
    renderList((async (input: RequestInfo | URL) => {
      const url = typeof input === "string" ? input : input.toString();
      const offset = url.includes("pageToken=t3")
        ? 2 * INSIGHTS_PAGE_SIZE
        : url.includes("pageToken=t2")
          ? INSIGHTS_PAGE_SIZE
          : 0;
      const reached = offset + INSIGHTS_PAGE_SIZE;
      return new Response(
        page(
          offset + 1,
          INSIGHTS_PAGE_SIZE,
          total,
          reached < total ? `t${reached / INSIGHTS_PAGE_SIZE + 1}` : null,
        ),
        { status: 200 },
      );
    }) as typeof fetch);

    await screen.findByText("issue 1");
    await userEvent.click(screen.getByRole("button", { name: /Next/i }));
    await screen.findByText("issue 11");
    await userEvent.click(screen.getByRole("button", { name: /Next/i }));

    expect(await screen.findByText("issue 21")).toBeInTheDocument();
    expect(screen.getByText(/Page 3 of 3 \(10 of 30\)/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Next/i })).toBeDisabled();
    expect(screen.getByRole("button", { name: /Previous/i })).toBeEnabled();
  });

  it("keeps the way back when a page comes back empty", async () => {
    // Reachable without a race: the issues a later page held can be dismissed
    // from another tab, or retired by a sweep, between the two reads. Previous
    // is the only way off such a page, so it cannot go missing with the rows.
    renderList((async (input: RequestInfo | URL) => {
      const url = typeof input === "string" ? input : input.toString();
      return new Response(
        url.includes("pageToken=t2")
          ? page(1, 0, 10, null)
          : page(1, INSIGHTS_PAGE_SIZE, 87, "t2"),
        { status: 200 },
      );
    }) as typeof fetch);

    await screen.findByText("issue 1");
    await userEvent.click(screen.getByRole("button", { name: /Next/i }));

    expect(
      await screen.findByText(/No insights found for this filter/),
    ).toBeInTheDocument();
    const previous = screen.getByRole("button", { name: /Previous/i });
    expect(previous).toBeEnabled();

    await userEvent.click(previous);
    expect(await screen.findByText("issue 1")).toBeInTheDocument();
  });

  it("shows no pager when everything matched fits on one page", async () => {
    renderList(serving(page(1, 3, 3, null)));

    await screen.findByText("issue 1");
    expect(screen.queryByText(/Page 1 of/)).toBeNull();
  });

  it("starts the next filter at its first page", async () => {
    const { urls } = renderList((async (input: RequestInfo | URL) => {
      const url = typeof input === "string" ? input : input.toString();
      return new Response(
        url.includes("pageToken=t2")
          ? page(11, INSIGHTS_PAGE_SIZE, 87, "t3")
          : page(1, INSIGHTS_PAGE_SIZE, 87, "t2"),
        { status: 200 },
      );
    }) as typeof fetch);

    await screen.findByText("issue 1");
    await userEvent.click(screen.getByRole("button", { name: /Next/i }));
    await screen.findByText("issue 11");
    await userEvent.click(screen.getByRole("button", { name: "Recurring" }));

    // A token addresses a row in the list it was issued for, so carrying it
    // into a narrower one would open that list part-way down.
    await waitFor(() => {
      expect(listReads(urls).some((u) => u.includes("status=RECURRING"))).toBe(
        true,
      );
    });
    const filtered = listReads(urls).filter((u) =>
      u.includes("status=RECURRING"),
    );
    expect(filtered.every((u) => !u.includes("pageToken"))).toBe(true);
  });
});
