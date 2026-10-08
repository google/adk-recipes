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
 * The `/insights` page keeps its status filter in the URL.
 *
 * Mounted through the real route, so what is pinned is the whole loop: the
 * URL a link or a reload arrives with, the tab that reads it, and the URL a
 * tab click leaves behind. The run detail embeds the same list and must not
 * take part in that loop at all.
 */
import { afterEach, describe, expect, it, vi } from "vitest";
import type { ReactNode } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import {
  createMemoryHistory,
  createRootRoute,
  createRoute,
  createRouter,
  RouterProvider,
} from "@tanstack/react-router";
import { INSIGHTS_PAGE_SIZE, type InsightView } from "@/lib/aqua-api";
import { Route as InsightsRoute } from "@/src/routes/insights.index";
import { InsightsView } from "../insights-view";

// The sidebar reads chat sessions over A2A, which is not what is under test.
vi.mock("@/components/nav/page-shell", () => ({
  PageShell: ({ children }: { children: ReactNode }) => <>{children}</>,
}));

const insight = (n: number): InsightView => ({
  insight_id: `ins-${n}`,
  agent_name: "it_support_agent",
  label: `insight ${n}`,
  tool_name: "create_ticket",
  status: "RECURRING",
  occurrence_count: 1,
  trace_count: n,
  impact: n,
  last_run_at: "2026-08-28T18:35:34Z",
  diagnosis: "",
  confidence: 1,
});

/** First page of 87 with a token to the second; the second page on `t2`. */
async function twoPages(input: RequestInfo | URL): Promise<Response> {
  const url = typeof input === "string" ? input : input.toString();
  const first = url.includes("pageToken=t2") ? 11 : 1;
  return new Response(
    JSON.stringify({
      insights: Array.from({ length: INSIGHTS_PAGE_SIZE }, (_, i) =>
        insight(first + i),
      ),
      total: 87,
      next_page_token: first === 1 ? "t2" : "t3",
    }),
    { status: 200 },
  );
}

function stubFetch(): string[] {
  const urls: string[] = [];
  vi.stubGlobal("fetch", ((input: RequestInfo | URL) => {
    urls.push(typeof input === "string" ? input : input.toString());
    return twoPages(input);
  }) as typeof fetch);
  return urls;
}

/** The list reads, in the order they were made. */
const listReads = (urls: string[]) =>
  urls.filter((u) => u.startsWith("/api/insights"));

function mount(
  routeTree: Parameters<typeof createRouter>[0]["routeTree"],
  at: string,
) {
  const router = createRouter({
    routeTree,
    history: createMemoryHistory({ initialEntries: [at] }),
  });
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: 0 } },
  });
  render(
    <QueryClientProvider client={queryClient}>
      {/* biome-ignore lint/suspicious/noExplicitAny: the test router is not the app's registered router type. */}
      <RouterProvider router={router as any} />
    </QueryClientProvider>,
  );
  return router;
}

/** The real `/insights` route, wired the way `routeTree.gen.ts` wires it. */
function mountPage(at: string) {
  const root = createRootRoute();
  const page = InsightsRoute.update({
    id: "/insights/",
    path: "/insights/",
    getParentRoute: () => root,
    // biome-ignore lint/suspicious/noExplicitAny: the test route tree is not the app's registered route tree.
  } as any);
  const detail = createRoute({
    getParentRoute: () => root,
    path: "/insights/$insightId",
    component: () => <div>Detail</div>,
  });
  return mount(root.addChildren([page, detail]), at);
}

const validate = InsightsRoute.options.validateSearch as (
  search: Record<string, unknown>,
) => Record<string, unknown>;

const tab = (name: string) => screen.getByRole("button", { name });

describe("the /insights status param", () => {
  it("keeps each status the list can narrow to", () => {
    for (const status of ["new", "recurring", "resolved"]) {
      expect(validate({ status }).status).toBe(status);
    }
  });

  it("drops a status the list cannot narrow to, rather than matching nothing", () => {
    for (const status of ["open", "dismissed", "RECURRING", "all", "", 3]) {
      expect(validate({ status }).status).toBeUndefined();
    }
  });

  it("reads a missing status as all of them", () => {
    expect(validate({})).toEqual({
      runId: undefined,
      status: undefined,
      day: undefined,
    });
  });
});

describe("the /insights day param", () => {
  it("keeps a real day and drops anything else", () => {
    expect(validate({ day: "2026-09-20" }).day).toBe("2026-09-20");
    for (const day of ["2026-02-30", "foo", 20260920, ""]) {
      expect(validate({ day }).day).toBeUndefined();
    }
  });

  it("drops recurring on a day, which no day is stamped with", () => {
    expect(validate({ day: "2026-09-20", status: "recurring" })).toEqual({
      runId: undefined,
      status: undefined,
      day: "2026-09-20",
    });
    expect(validate({ day: "2026-09-20", status: "resolved" }).status).toBe(
      "resolved",
    );
  });
});

describe("the /insights page", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("opens on the status the URL names, and asks the engine for it", async () => {
    const urls = stubFetch();
    mountPage("/insights?status=recurring");

    await screen.findByText("insight 1");
    expect(tab("Recurring")).toHaveAttribute("aria-pressed", "true");
    expect(tab("All")).toHaveAttribute("aria-pressed", "false");
    expect(listReads(urls).every((u) => u.includes("status=RECURRING"))).toBe(
      true,
    );
  });

  it("opens on all of them when the URL names a status it does not have", async () => {
    const urls = stubFetch();
    mountPage("/insights?status=open");

    await screen.findByText("insight 1");
    expect(tab("All")).toHaveAttribute("aria-pressed", "true");
    expect(listReads(urls).every((u) => !u.includes("status="))).toBe(true);
  });

  it("writes a tab click into the URL, and all of them as no param", async () => {
    stubFetch();
    const router = mountPage("/insights");
    await screen.findByText("insight 1");
    const entries = router.history.length;

    await userEvent.click(tab("Resolved"));
    await waitFor(() =>
      expect(router.state.location.search).toEqual({ status: "resolved" }),
    );
    expect(tab("Resolved")).toHaveAttribute("aria-pressed", "true");

    await userEvent.click(tab("All"));
    await waitFor(() => expect(router.state.location.searchStr).toBe(""));
    expect(tab("All")).toHaveAttribute("aria-pressed", "true");
    // Replaced, not pushed: Back leaves the page rather than stepping
    // through every tab that was tried on it.
    expect(router.history.length).toBe(entries);
  });

  it("keeps the other params when a tab changes the status", async () => {
    const urls = stubFetch();
    const router = mountPage("/insights?runId=run-7");
    await screen.findByText("insight 1");

    await userEvent.click(tab("New"));

    await waitFor(() =>
      expect(router.state.location.search).toEqual({
        runId: "run-7",
        status: "new",
      }),
    );
    const last = listReads(urls).at(-1) ?? "";
    expect(last).toContain("runId=run-7");
    expect(last).toContain("status=NEW");
  });

  it("goes back to the first page when the selected tab is clicked again", async () => {
    const urls = stubFetch();
    mountPage("/insights?status=recurring");
    await screen.findByText("insight 1");
    await userEvent.click(screen.getByRole("button", { name: /Next/i }));
    await screen.findByText("insight 11");

    await userEvent.click(tab("Recurring"));

    await screen.findByText("insight 1");
    expect(listReads(urls).at(-1)).not.toContain("pageToken");
  });

  it("follows the URL when it changes under a mounted page", async () => {
    // The sidebar's "All insights" link, or Back, while this page is open.
    const urls = stubFetch();
    const router = mountPage("/insights?status=new");
    await screen.findByText("insight 1");
    await userEvent.click(screen.getByRole("button", { name: /Next/i }));
    await screen.findByText("insight 11");

    await act(() => router.navigate({ to: "/insights", search: {} }));

    await waitFor(() =>
      expect(tab("All")).toHaveAttribute("aria-pressed", "true"),
    );
    await screen.findByText("insight 1");
    // The token was issued for the `new` list; carried over, it would open
    // the unfiltered list part-way down.
    const unfiltered = listReads(urls).filter((u) => !u.includes("status="));
    expect(unfiltered.length).toBeGreaterThan(0);
    expect(unfiltered.every((u) => !u.includes("pageToken"))).toBe(true);
  });
});

/** One page of `count` insights out of `count`: a day's list, all of it. */
function stubDay(count: number): string[] {
  const urls: string[] = [];
  vi.stubGlobal("fetch", ((input: RequestInfo | URL) => {
    urls.push(typeof input === "string" ? input : input.toString());
    return Promise.resolve(
      new Response(
        JSON.stringify({
          insights: Array.from({ length: count }, (_, i) => insight(i + 1)),
          total: count,
          next_page_token: null,
        }),
        { status: 200 },
      ),
    );
  }) as typeof fetch);
  return urls;
}

describe("the /insights page narrowed to a day", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("asks the engine for the day, so the list and its count are that day's", async () => {
    const urls = stubDay(3);
    mountPage("/insights?day=2026-09-20&status=new");

    await screen.findByText("insight 1");
    expect(listReads(urls).every((u) => u.includes("day=2026-09-20"))).toBe(
      true,
    );
    expect(listReads(urls).every((u) => u.includes("status=NEW"))).toBe(true);
    expect(
      screen.getByText(/^3 insights found on 2026-09-20 \(UTC\) ·/),
    ).toBeInTheDocument();
    // A day's columns are new and resolved; nothing recurs on a day.
    expect(screen.queryByRole("button", { name: "Recurring" })).toBeNull();
  });

  it("says which day came back empty, and that the day is UTC", async () => {
    stubDay(0);
    mountPage("/insights?day=2026-09-20&status=resolved");

    expect(
      await screen.findByText("No insights were resolved on 2026-09-20 (UTC)."),
    ).toBeInTheDocument();
  });

  it("clears the day with one button, keeps the status, and hands focus to the tabs", async () => {
    stubDay(3);
    const router = mountPage("/insights?day=2026-09-20&status=new");
    await screen.findByText("insight 1");

    await userEvent.click(
      screen.getByRole("button", {
        name: "Day 2026-09-20 (UTC), remove filter",
      }),
    );

    await waitFor(() =>
      expect(router.state.location.search).toEqual({ status: "new" }),
    );
    expect(screen.queryByRole("button", { name: /remove filter/ })).toBeNull();
    expect(screen.getByRole("group", { name: "Status" })).toContainElement(
      document.activeElement as HTMLElement,
    );
  });

  it("names both changes under the all tab", async () => {
    stubDay(4);
    mountPage("/insights?day=2026-09-20");

    await screen.findByText("insight 1");
    expect(
      screen.getByText(/^4 insights found or resolved on 2026-09-20 \(UTC\) ·/),
    ).toBeInTheDocument();
  });

  it("starts another day from its first page", async () => {
    const urls = stubFetch();
    const router = mountPage("/insights?day=2026-09-20");
    await screen.findByText("insight 1");
    await userEvent.click(screen.getByRole("button", { name: /Next/i }));
    await screen.findByText("insight 11");

    await act(() =>
      router.navigate({ to: "/insights", search: { day: "2026-09-21" } }),
    );

    await screen.findByText("insight 1");
    const otherDay = listReads(urls).filter((u) =>
      u.includes("day=2026-09-21"),
    );
    expect(otherDay.length).toBeGreaterThan(0);
    // The token was issued for the 20th's list.
    expect(otherDay.every((u) => !u.includes("pageToken"))).toBe(true);
  });

  it("keeps the day when a tab is clicked", async () => {
    stubDay(3);
    const router = mountPage("/insights?day=2026-09-20");
    await screen.findByText("insight 1");

    await userEvent.click(tab("Resolved"));

    await waitFor(() =>
      expect(router.state.location.search).toEqual({
        day: "2026-09-20",
        status: "resolved",
      }),
    );
  });
});

describe("the insights list embedded in another page", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("filters in place and leaves that page's URL alone", async () => {
    const urls = stubFetch();
    const root = createRootRoute();
    const runDetail = createRoute({
      getParentRoute: () => root,
      path: "/investigations/$runId",
      component: () => <InsightsView runId="run-1" embedded />,
    });
    const router = mount(
      root.addChildren([runDetail]),
      "/investigations/run-1",
    );
    await screen.findByText("insight 1");

    await userEvent.click(tab("Recurring"));

    await waitFor(() =>
      expect(listReads(urls).some((u) => u.includes("status=RECURRING"))).toBe(
        true,
      ),
    );
    expect(tab("Recurring")).toHaveAttribute("aria-pressed", "true");
    expect(router.state.location.href).toBe("/investigations/run-1");
  });
});
