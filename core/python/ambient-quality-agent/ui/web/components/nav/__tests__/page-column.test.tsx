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
 * Every dashboard page lays its content out in the column PageShell draws:
 * 80% of main on Home and Investigations (width="wide") and 65% on every
 * other page (width="narrow", the default), full-width below md.
 *
 * Mounted through the real routes and the real PageShell, so a page that sets
 * a width of its own fails here.
 */
import type { ReactNode } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import {
  createMemoryHistory,
  createRootRoute,
  createRoute,
  createRouter,
  RouterProvider,
} from "@tanstack/react-router";
import { afterEach, describe, expect, it, vi } from "vitest";

import { Route as ConfigRoute } from "@/src/routes/config";
import { Route as HomeRoute } from "@/src/routes/index";
import { Route as InsightRoute } from "@/src/routes/insights.$insightId";
import { Route as InsightsRoute } from "@/src/routes/insights.index";
import { Route as CaseRoute } from "@/src/routes/investigations.$runId.cases.$caseId";
import { Route as InvestigationRoute } from "@/src/routes/investigations.$runId.index";
import { Route as InvestigationsRoute } from "@/src/routes/investigations.index";

// The rail and the chat sessions it lists over A2A are not what is under test.
vi.mock("@/components/nav/sidebar-rail", () => ({
  SidebarRail: ({ children }: { children: ReactNode }) => <>{children}</>,
}));
vi.mock("@/lib/horizon-sessions", () => ({
  useLhaSessions: () => ({ data: [], isLoading: false, refresh: () => {} }),
}));

const WIDE_COLUMN = ["md:w-[80%]", "mx-auto", "p-6", "w-full"];
const NARROW_COLUMN = ["md:w-[65%]", "mx-auto", "p-6", "w-full"];

const RUN = {
  run_id: "run-12345678",
  status: "done",
  observed_agent_name: "travel_desk_agent",
  created_at: "2026-09-14T06:00:00Z",
  events: [],
  summary: {},
};

function answer(url: string): Promise<Response> {
  const json = (body: unknown, status = 200) =>
    Promise.resolve(new Response(JSON.stringify(body), { status }));
  if (url.includes("/api/investigations/pending")) return new Promise(() => {});
  if (/\/api\/investigations\/[^/?]+\/cases\//.test(url))
    return new Promise(() => {});
  if (/\/api\/investigations\/[^/?]+/.test(url)) return json({ run: RUN });
  if (url.includes("/api/insights/broken")) return json({ error: "boom" }, 500);
  if (/\/api\/insights\/[^/?]+/.test(url)) {
    return json({
      insight: {
        insight_id: "ins-1",
        agent_name: "travel_desk_agent",
        label: "the refund tool is never called",
        status: "NEW",
        occurrence_count: 1,
        trace_count: 1,
        last_run_at: "2026-09-14T06:00:00Z",
      },
    });
  }
  if (url.includes("/api/insights")) return json({ insights: [] });
  // Everything else stays in flight: those pages draw their layout before any
  // of their data arrives.
  return new Promise(() => {});
}

function mount(at: string) {
  vi.stubGlobal("fetch", vi.fn(answer));
  const root = createRootRoute();
  const pages = [
    [HomeRoute, "/"],
    [InvestigationsRoute, "/investigations/"],
    [InvestigationRoute, "/investigations/$runId/"],
    [CaseRoute, "/investigations/$runId/cases/$caseId"],
    [InsightsRoute, "/insights/"],
    [InsightRoute, "/insights/$insightId"],
    [ConfigRoute, "/config"],
  ] as const;
  const routes = pages.map(([route, path]) =>
    // biome-ignore lint/suspicious/noExplicitAny: the test route tree is not the app's registered route tree.
    route.update({ id: path, path, getParentRoute: () => root } as any),
  );
  const chat = createRoute({ getParentRoute: () => root, path: "/c" });
  const router = createRouter({
    // biome-ignore lint/suspicious/noExplicitAny: the test route tree is not the app's registered route tree.
    routeTree: root.addChildren([...(routes as any[]), chat]),
    history: createMemoryHistory({ initialEntries: [at] }),
  });
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      {/* biome-ignore lint/suspicious/noExplicitAny: the test router is not the app's registered router type. */}
      <RouterProvider router={router as any} />
    </QueryClientProvider>,
  );
}

/** The classes that decide where content sits and how wide it gets. */
function layout(el: Element | null | undefined): string[] {
  return (el?.className ?? "")
    .split(/\s+/)
    .filter((c) =>
      /^(mx-auto|w-|md:w-|max-w-|md:max-w-|p-|px-|py-|pt-|pb-)/.test(c),
    )
    .sort();
}

describe("the page column", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it.each([
    ["Home", "/", WIDE_COLUMN, undefined],
    ["Investigations", "/investigations", WIDE_COLUMN, undefined],
    [
      "an investigation",
      "/investigations/run-12345678",
      NARROW_COLUMN,
      "All investigations",
    ],
    [
      "an investigation still loading",
      "/investigations/pending0",
      NARROW_COLUMN,
      "Loading investigation pending0…",
    ],
    [
      "a trajectory",
      "/investigations/run-12345678/cases/case-1",
      NARROW_COLUMN,
      undefined,
    ],
    ["Insights", "/insights", NARROW_COLUMN, undefined],
    ["an insight", "/insights/ins-1", NARROW_COLUMN, "All insights"],
    [
      "an insight that failed to load",
      "/insights/broken",
      NARROW_COLUMN,
      "Couldn't load this insight",
    ],
    ["Configuration", "/config", NARROW_COLUMN, undefined],
  ])("is the only one on %s", async (_page, at, expectedColumn, ready) => {
    const { container } = mount(at);
    if (ready) await screen.findByText(ready, { exact: false });
    await waitFor(() => {
      const column = container.querySelector("main")?.firstElementChild;
      expect(layout(column)).toEqual(expectedColumn);
      // A page capping its own width inside the column is the drift this
      // guards against.
      const capped = [...(column?.querySelectorAll("[class*='max-w-']") ?? [])]
        .map((el) => layout(el).find((c) => /^max-w-[2-7]xl$/.test(c)))
        .filter(Boolean);
      expect(capped).toEqual([]);
    });
  });
});
