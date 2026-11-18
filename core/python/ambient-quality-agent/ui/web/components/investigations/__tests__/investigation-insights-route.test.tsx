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
 * The investigation detail keeps its insights list's status tab and page in the
 * URL.
 *
 * Mounted through the real route, so Back from an insight opened on page 2 of
 * the investigation's list lands on page 2 again rather than on the first page.
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
import {
  INSIGHTS_PAGE_SIZE,
  type InsightView,
  type Investigation,
} from "@/lib/aqua-api";
import { counters } from "@/lib/__fixtures__/counters";
import { Route as InvestigationRoute } from "@/src/routes/investigations.$investigationId.index";

// The sidebar reads chat sessions over A2A, which is not what is under test.
vi.mock("@/components/nav/page-shell", () => ({
  PageShell: ({ children }: { children: ReactNode }) => <>{children}</>,
}));

const INVESTIGATION: Investigation = {
  investigation_id: "inv-12345678",
  status: "done",
  observed_agent_key: "it_support_agent",
  trigger_type: "manual",
  created_at: "2026-08-28T18:35:34Z",
  elapsed_seconds: 42.5,
  metrics_passed: 10,
  metrics_failed: 2,
  metrics_errored: 0,
  counters: counters({
    trajectories_evaluated: 50,
    trajectories_eval_passed: 40,
  }),
  summary: {},
  events: [],
};

const insight = (n: number): InsightView => ({
  insight_id: `ins-${n}`,
  observed_agent_key: "it_support_agent",
  label: `insight ${n}`,
  tool_name: "create_ticket",
  status: "RECURRING",
  occurrence_count: 1,
  trajectory_count: n,
  impact: n,
  last_investigation_at: "2026-08-28T18:35:34Z",
  diagnosis: "",
  confidence: 1,
});

/** Serves the investigation, and 87 insights of which `page=2` is the
 *  second ten. */
function stubFetch(): string[] {
  const urls: string[] = [];
  vi.stubGlobal("fetch", ((input: RequestInfo | URL) => {
    const url = typeof input === "string" ? input : input.toString();
    urls.push(url);
    if (!url.startsWith("/api/insights")) {
      return Promise.resolve(
        new Response(JSON.stringify({ investigation: INVESTIGATION }), {
          status: 200,
        }),
      );
    }
    const first = url.includes("page=2") ? 11 : 1;
    return Promise.resolve(
      new Response(
        JSON.stringify({
          insights: Array.from({ length: INSIGHTS_PAGE_SIZE }, (_, i) =>
            insight(first + i),
          ),
          total: 87,
          next_page_token: "next",
        }),
        { status: 200 },
      ),
    );
  }) as typeof fetch);
  return urls;
}

function mountPage(at: string) {
  const root = createRootRoute();
  const page = InvestigationRoute.update({
    id: "/investigations/$investigationId/",
    path: "/investigations/$investigationId/",
    getParentRoute: () => root,
    // biome-ignore lint/suspicious/noExplicitAny: the test route tree is not the app's registered route tree.
  } as any);
  const detail = createRoute({
    getParentRoute: () => root,
    path: "/insights/$insightId",
    component: () => <div>Detail</div>,
  });
  const router = createRouter({
    routeTree: root.addChildren([page, detail]),
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

const validate = InvestigationRoute.options.validateSearch as (
  search: Record<string, unknown>,
) => Record<string, unknown>;

describe("the investigation detail's insights params", () => {
  it("keeps a status the list has and a page above the first", () => {
    expect(validate({ status: "new", page: "3" })).toEqual({
      status: "new",
      page: 3,
    });
  });

  it("drops what the list cannot draw", () => {
    expect(validate({ status: "open", page: "1" })).toEqual({
      status: undefined,
      page: undefined,
    });
  });
});

describe("the investigation detail's insights list", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("returns to the same page after opening an insight and going Back", async () => {
    const urls = stubFetch();
    const router = mountPage("/investigations/inv-12345678");
    await screen.findByText("insight 1");

    await userEvent.click(screen.getByRole("button", { name: /Next/i }));
    await screen.findByText("insight 11");
    expect(router.state.location.search).toEqual({ page: 2 });

    await userEvent.click(screen.getByText("insight 11"));
    await screen.findByText("Detail");
    await act(() => router.history.back());

    expect(await screen.findByText("insight 11")).toBeInTheDocument();
    const read = urls.filter((u) => u.startsWith("/api/insights")).at(-1);
    expect(read).toContain("investigationId=inv-12345678");
    expect(read).toContain("page=2");
  });

  it("keeps the status tab with the page, and starts a new tab at its first page", async () => {
    stubFetch();
    const router = mountPage("/investigations/inv-12345678?status=new&page=2");
    expect(await screen.findByText("insight 11")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "New" })).toHaveAttribute(
      "aria-pressed",
      "true",
    );

    await userEvent.click(screen.getByRole("button", { name: "Resolved" }));

    await waitFor(() =>
      expect(router.state.location.search).toEqual({ status: "resolved" }),
    );
    expect(await screen.findByText("insight 1")).toBeInTheDocument();
  });
});
