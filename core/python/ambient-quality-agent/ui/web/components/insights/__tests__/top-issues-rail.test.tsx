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
 * The rail asks the engine for `RAIL_SIZE` rows already ranked and cut: the
 * request carries `pageSize=10` and `orderBy=impact`. Its header counts every
 * open issue and links to all of them. An empty page is said, not hidden, and
 * says whether nothing is failing or nothing has been analysed yet.
 */
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import {
  RouterProvider,
  createMemoryHistory,
  createRootRoute,
  createRoute,
  createRouter,
} from "@tanstack/react-router";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { InsightView } from "@/lib/aqua-api";
import { resetRailScrollForTest } from "@/components/chat/use-rail-scroll";
import { TopIssuesRail } from "../top-issues-rail";

function insight(patch: Partial<InsightView>): InsightView {
  return {
    insight_id: "i1",
    agent_name: "a",
    label: "calls file_expense with a category outside the accepted set",
    tool_name: "t",
    status: "RECURRING",
    occurrence_count: 1,
    trace_count: 715,
    impact: 1,
    last_run_at: null,
    diagnosis: "",
    confidence: 1,
    ...patch,
  };
}

interface Payloads {
  insights?: InsightView[];
  total?: number;
  runs?: unknown[];
  status?: number;
}

function renderRail(
  { insights = [], total, runs = [], status = 200 }: Payloads = {},
  path = "/",
) {
  const fetchMock = vi.fn<typeof fetch>(async (input) => {
    const url = String(input);
    if (status !== 200) return new Response("{}", { status });
    if (url.includes("/api/investigations")) {
      return new Response(JSON.stringify({ runs }), { status: 200 });
    }
    return new Response(
      JSON.stringify({ insights, total: total ?? insights.length }),
      { status: 200 },
    );
  });
  vi.stubGlobal("fetch", fetchMock);

  const root = createRootRoute({ component: TopIssuesRail });
  const router = createRouter({
    routeTree: root.addChildren(
      ["/", "/insights", "/insights/$insightId", "/c"].map((p) =>
        createRoute({ getParentRoute: () => root, path: p }),
      ),
    ),
    history: createMemoryHistory({ initialEntries: [path] }),
  });
  render(
    <QueryClientProvider
      client={
        new QueryClient({
          defaultOptions: { queries: { retry: false, gcTime: 0 } },
        })
      }
    >
      <RouterProvider router={router} />
    </QueryClientProvider>,
  );
  return fetchMock;
}

const rowLink = (text: string | RegExp) =>
  screen.findByRole("link", { name: text });

beforeEach(() => resetRailScrollForTest());
afterEach(() => vi.unstubAllGlobals());

describe("TopIssuesRail", () => {
  it("asks the engine for the ten worst issues", async () => {
    const fetchMock = renderRail();

    await waitFor(() => expect(fetchMock).toHaveBeenCalled());
    const url = String(fetchMock.mock.calls[0]?.[0]);
    expect(url).toContain("pageSize=10");
    expect(url).toContain("orderBy=impact");
  });

  it("says no insights exist yet when nothing has been evaluated", async () => {
    renderRail();

    expect(await screen.findByText("No insights yet.")).toBeInTheDocument();
  });

  it("takes no share of the rail for its empty line", async () => {
    renderRail();
    await screen.findByText("No insights yet.");

    expect(
      screen.getByRole("region", { name: "Top insights" }).className,
    ).not.toContain("flex-1");
  });

  it("takes no share of the rail for its error line", async () => {
    renderRail({ status: 500 });
    await screen.findByRole("button", { name: "Retry" });

    expect(
      screen.getByRole("region", { name: "Top insights" }).className,
    ).not.toContain("flex-1");
  });

  it("says nothing is failing when runs evaluated traces and found no issue", async () => {
    renderRail({
      runs: [
        {
          run_id: "r1",
          created_at: "2026-09-14T00:00:00Z",
          status: "done",
          counters: { traces_eval_passed: 40 },
        },
      ],
    });

    expect(
      await screen.findByText("Nothing failing right now."),
    ).toBeInTheDocument();
  });

  it("counts every open issue in its header and links to all of them", async () => {
    renderRail({ insights: [insight({})], total: 42 });

    expect(
      await screen.findByRole("heading", { level: 2, name: "Top insights 42" }),
    ).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "All insights" })).toHaveAttribute(
      "href",
      "/insights",
    );
  });

  it("shows traces first, then whether the issue is diagnosed", async () => {
    renderRail({
      insights: [
        insight({ insight_id: "a", trace_count: 715 }),
        insight({
          insight_id: "b",
          label: "big",
          trace_count: 1_234_567,
          diagnosis: "why",
        }),
      ],
    });

    const first = await rowLink(/calls file_expense/);
    expect(first).toHaveTextContent("715 trajectories · undiagnosed");
    const second = await rowLink(/why/);
    expect(second).toHaveTextContent("1.2M trajectories");
    expect(second).not.toHaveTextContent("undiagnosed");
  });

  it("gives a long title a tooltip, its own direction and room to wrap", async () => {
    const long = "https://example.com/" + "x".repeat(200);
    renderRail({ insights: [insight({ label: long })] });

    const row = await rowLink(/example/);
    const title = within(row).getByText(long);
    expect(title).toHaveAttribute("title", long);
    expect(title).toHaveAttribute("dir", "auto");
    expect(title.className).toContain("break-words");
  });

  it("names an insight with a blank label", async () => {
    renderRail({ insights: [insight({ label: "   " })] });

    expect(await rowLink(/Untitled insight/)).toBeInTheDocument();
  });

  it("keeps the diagnose button beside the row link, at least 24px square", async () => {
    renderRail({ insights: [insight({})] });

    const row = await rowLink(/calls file_expense/);
    const diagnose = screen.getByRole("button", {
      name: /Diagnose insight in chat/,
    });
    expect(row).not.toContainElement(diagnose);
    expect(diagnose.className).toMatch(/\bh-6\b/);
    expect(diagnose.className).toMatch(/\bw-6\b/);
  });

  it("marks the current route's insight", async () => {
    renderRail(
      {
        insights: [
          insight({ insight_id: "a" }),
          insight({ insight_id: "b", label: "other" }),
        ],
      },
      "/insights/b",
    );

    const active = await rowLink(/other/);
    expect(active).toHaveAttribute("aria-current", "page");
    expect(active.className).toContain("bg-accent");
    // No side bar: an inset shadow bends around the rounded corners.
    expect(active.className).not.toContain("shadow-");
  });

  it("reserves its rows with a skeleton while loading", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(() => new Promise<Response>(() => {})),
    );
    const root = createRootRoute({ component: TopIssuesRail });
    const router = createRouter({
      routeTree: root.addChildren([
        createRoute({ getParentRoute: () => root, path: "/" }),
      ]),
      history: createMemoryHistory({ initialEntries: ["/"] }),
    });
    render(
      <QueryClientProvider client={new QueryClient()}>
        <RouterProvider router={router} />
      </QueryClientProvider>,
    );

    expect(
      await screen.findByRole("status", { name: /loading top insights/i }),
    ).toBeInTheDocument();
  });

  it("offers a retry when loading failed", async () => {
    const fetchMock = renderRail({ status: 500 });

    await userEvent.click(await screen.findByRole("button", { name: "Retry" }));

    await waitFor(() =>
      expect(
        fetchMock.mock.calls.filter(([u]) =>
          String(u).includes("/api/insights"),
        ),
      ).toHaveLength(2),
    );
  });
});
