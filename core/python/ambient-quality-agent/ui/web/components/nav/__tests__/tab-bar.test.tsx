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

import { describe, expect, it, vi, beforeEach } from "vitest";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import {
  act,
  render,
  renderHook,
  screen,
  waitFor,
} from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import {
  createMemoryHistory,
  createRootRoute,
  createRoute,
  createRouter,
  RouterProvider,
} from "@tanstack/react-router";
import { publishRailState } from "@/lib/rail-state";
import { endTour, useTourStep } from "@/lib/tour-state";
import { TabBar } from "../tab-bar";

/** Answers every AQuA endpoint; `health` overrides just `/api/health`. */
function stubApi(health: unknown, runs: unknown[] = []): typeof fetch {
  return ((input: RequestInfo | URL) => {
    const url = typeof input === "string" ? input : input.toString();
    if (url.includes("/api/health")) {
      return Promise.resolve(
        new Response(JSON.stringify(health), { status: 200 }),
      );
    }
    if (url.includes("/api/investigations")) {
      return Promise.resolve(
        new Response(JSON.stringify({ runs }), { status: 200 }),
      );
    }
    return Promise.resolve(
      new Response(JSON.stringify({ insights: [], total: 0 }), { status: 200 }),
    );
  }) as typeof fetch;
}

const aRun = () => ({
  run_id: "run-123",
  observed_agent_name: "it_support_agent",
  created_at: new Date(Date.now() - 3600000).toISOString(),
  finished_at: new Date(Date.now() - 3500000).toISOString(),
});

function renderBar(fetchImpl: typeof fetch) {
  vi.stubGlobal("fetch", fetchImpl);
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: 0 } },
  });

  const rootRoute = createRootRoute();
  const indexRoute = createRoute({
    getParentRoute: () => rootRoute,
    path: "/",
    component: () => <TabBar />,
  });
  const history = createMemoryHistory({ initialEntries: ["/"] });
  const router = createRouter({
    routeTree: rootRoute.addChildren([indexRoute]),
    history,
  });

  return render(
    <QueryClientProvider client={queryClient}>
      <RouterProvider router={router} />
    </QueryClientProvider>,
  );
}

describe("TabBar", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    publishRailState({ collapsed: false, toggle: null });
  });

  it("renders the brand and the observed agent", async () => {
    renderBar(((input: RequestInfo | URL) => {
      const url = typeof input === "string" ? input : input.toString();
      if (url.includes("/api/investigations")) {
        return Promise.resolve(
          new Response(
            JSON.stringify({
              runs: [
                {
                  run_id: "run-123",
                  observed_agent_name: "it_support_agent",
                  created_at: new Date(Date.now() - 3600000).toISOString(),
                  finished_at: new Date(Date.now() - 3500000).toISOString(),
                },
              ],
            }),
            { status: 200 },
          ),
        );
      }
      return Promise.resolve(
        new Response(JSON.stringify({ insights: [], total: 0 }), {
          status: 200,
        }),
      );
    }) as typeof fetch);

    expect(await screen.findByText("AQuA")).toBeInTheDocument();
    expect(await screen.findByText("it_support_agent")).toBeInTheDocument();
    // Freshness moved to the homepage stat bar, which reads it from the health
    // endpoint. Two sources for one fact disagreed on screen.
    expect(screen.queryByText(/last run/)).toBeNull();
  });

  it("names the configured agent before any run or insight exists", async () => {
    // A new deployment has neither, and the badge used to fall back to a
    // hard-coded `it_support_agent` whatever AQuA was configured to observe.
    renderBar(((input: RequestInfo | URL) => {
      const url = typeof input === "string" ? input : input.toString();
      if (url.includes("/api/config")) {
        return Promise.resolve(
          new Response(
            JSON.stringify({
              config: { observed_agent_name: "travel_desk_agent" },
            }),
            { status: 200 },
          ),
        );
      }
      if (url.includes("/api/investigations")) {
        return Promise.resolve(
          new Response(JSON.stringify({ runs: [] }), { status: 200 }),
        );
      }
      return Promise.resolve(
        new Response(JSON.stringify({ insights: [], total: 0 }), {
          status: 200,
        }),
      );
    }) as typeof fetch);

    expect(await screen.findByText("travel_desk_agent")).toBeInTheDocument();
    expect(screen.queryByText("it_support_agent")).toBeNull();
  });

  // The dot outlives the homepage strip, so it is the last thing on screen
  // claiming AQuA is fine. It used to be an unconditional emerald pulse.
  it("does not pulse green when the schedule has never fired", async () => {
    renderBar(
      stubApi(
        {
          verdict: "never",
          reason: "no scheduled investigation has ever finished",
        },
        [aRun()],
      ),
    );

    // The dot renders before the health query resolves, so wait for the colour
    // rather than for the element.
    const dot = await screen.findByTestId("agent-health-dot");
    await waitFor(() => expect(dot.className).toMatch(/amber/));
    expect(dot.className).not.toMatch(/emerald/);
    // `cn` is twMerge, so a stray colour class would be overridden anyway --
    // the pulse is not a colour and would survive one. A paused scheduler
    // should not have a lively dot.
    expect(dot.className).not.toMatch(/animate-pulse/);
  });

  it("pulses green only when AQuA is actually watching", async () => {
    renderBar(
      stubApi({ verdict: "watching", reason: "ran 6 hours ago" }, [aRun()]),
    );

    const dot = await screen.findByTestId("agent-health-dot");
    await waitFor(() => expect(dot.className).toMatch(/emerald/));
  });

  // The control used to float at the top-left corner of the page, on top of
  // the wordmark: nothing between it and the viewport was positioned.
  it("offers the way back from a collapsed rail, after the wordmark", async () => {
    const toggle = vi.fn();
    renderBar(stubApi({ verdict: "watching" }, [aRun()]));
    act(() => publishRailState({ collapsed: true, toggle }));

    const wordmark = await screen.findByText("AQuA");
    const button = await screen.findByRole("button", { name: /show chats/i });
    expect(
      wordmark.compareDocumentPosition(button) &
        Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();

    await userEvent.click(button);
    expect(toggle).toHaveBeenCalled();
  });

  it("marks that control for the rail, which hands it focus", async () => {
    renderBar(stubApi({ verdict: "watching" }, [aRun()]));
    act(() => publishRailState({ collapsed: true, toggle: () => {} }));

    // The rail finds the button by this attribute, not by its label, which
    // is copy and can change.
    const button = await screen.findByRole("button", { name: /show chats/i });
    expect(button.hasAttribute("data-rail-toggle")).toBe(true);
  });

  it("hides that control while the rail is open", async () => {
    renderBar(stubApi({ verdict: "watching" }, [aRun()]));
    act(() => publishRailState({ collapsed: false, toggle: () => {} }));

    expect(await screen.findByText("AQuA")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /show chats/i })).toBeNull();
  });

  it("opens the guided tour", async () => {
    const tour = renderHook(() => useTourStep());
    renderBar(stubApi({ verdict: "watching" }, [aRun()]));

    await userEvent.click(
      await screen.findByRole("button", { name: "Take the tour" }),
    );
    expect(tour.result.current).toBe(0);
    act(() => endTour());
  });

  it("stays neutral while health is unknown", async () => {
    // An unreachable health check must not read as either healthy or broken.
    renderBar(stubApi({ error: "unreachable" }, [aRun()]));

    const dot = await screen.findByTestId("agent-health-dot");
    await waitFor(() => expect(dot.className).toMatch(/muted-foreground/));
    expect(dot.className).not.toMatch(/emerald|amber|red/);
  });
});
