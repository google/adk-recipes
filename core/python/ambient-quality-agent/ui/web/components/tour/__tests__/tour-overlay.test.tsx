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
 * The guided tour over a stand-in dashboard: every page the tour visits,
 * reduced to the elements its steps point at, so what is pinned is the walk
 * itself -- which step is open, which page it opens and how, and what it skips.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import {
  createMemoryHistory,
  createRootRoute,
  createRoute,
  createRouter,
  Link,
  Outlet,
  RouterProvider,
} from "@tanstack/react-router";
import { endTour, showTourStep, startTour } from "@/lib/tour-state";
import { placeCallout, TourOverlay } from "../tour-overlay";
import { HOME_SIDE_BY_SIDE, TOUR_STEPS } from "../tour-steps";

const stepAt = (target: string) =>
  TOUR_STEPS.findIndex((step) => step.target === target);
const titleOf = (target: string) => TOUR_STEPS[stepAt(target)].title;
const callout = () => screen.queryByRole("dialog");
/** Long enough for the pointer to travel and click, or for a missing element's
 *  grace to run out. */
const SLOW = { timeout: 3000 };

/** Answers the two reads the tour makes: the top insight and the runs. */
function stubApi({ insights }: { insights: boolean }): typeof fetch {
  return ((input: RequestInfo | URL) => {
    const url = typeof input === "string" ? input : input.toString();
    const body = url.includes("/api/investigations")
      ? {
          runs: [
            {
              run_id: "run-done",
              status: "done",
              created_at: "2026-09-28T08:00:00Z",
            },
            {
              run_id: "run-running",
              status: "running",
              created_at: "2026-09-29T08:00:00Z",
            },
          ],
        }
      : {
          insights: insights
            ? [{ insight_id: "ins-1", label: "invents a priority" }]
            : [],
          total: insights ? 1 : 0,
        };
    return Promise.resolve(new Response(JSON.stringify(body), { status: 200 }));
  }) as typeof fetch;
}

/**
 * Renders the stand-in dashboard. `links` adds the links a reader would press
 * between its pages -- the sidebar's "All", Home's "View all" and an icon for
 * the latest investigation -- so the tour has a way to show; without them it
 * changes pages directly.
 */
function renderDashboard({
  at = "/",
  insights = true,
  links = false,
}: {
  at?: string;
  insights?: boolean;
  links?: boolean;
} = {}) {
  vi.stubGlobal("fetch", stubApi({ insights }));
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: 0 } },
  });
  const rootRoute = createRootRoute({
    component: () => (
      <>
        <button type="button" data-tour="tour-button">
          Take the tour
        </button>
        {links && (
          <Link
            to="/investigations/$runId"
            params={{ runId: "run-done" }}
            aria-label="Latest investigation"
          >
            🔬
          </Link>
        )}
        {links && (
          <nav>
            <Link to="/insights">All</Link>
          </nav>
        )}
        <main>
          <Outlet />
        </main>
        <TourOverlay />
      </>
    ),
  });
  const page = (path: string, targets: string[], extra?: () => JSX.Element) =>
    createRoute({
      getParentRoute: () => rootRoute,
      path,
      component: () => (
        <>
          {targets.map((target) => (
            <div key={target} data-tour={target} />
          ))}
          {extra?.()}
        </>
      ),
    });
  const router = createRouter({
    routeTree: rootRoute.addChildren([
      page(
        "/",
        ["health", "ask", "insights-strip", "pipeline", "recent-runs"],
        () => (links ? <Link to="/insights">View all</Link> : <></>),
      ),
      page("/insights", ["insights-triage"]),
      page("/insights/$insightId", ["insight-evidence", "insight-actions"]),
      page("/investigations/$runId", ["run-header"]),
    ]),
    history: createMemoryHistory({ initialEntries: [at] }),
  });
  render(
    <QueryClientProvider client={client}>
      <RouterProvider router={router} />
    </QueryClientProvider>,
  );
  return router;
}

/** Gives every marked element and every link a box, and those named in
 *  `boxes` the box given, standing in for the layout this environment does
 *  not do: an element without a size counts as not on screen. */
function sizeElements(boxes: Record<string, DOMRect> = {}) {
  vi.spyOn(Element.prototype, "getBoundingClientRect").mockImplementation(
    function (this: Element) {
      const target = this.getAttribute("data-tour");
      if (target && boxes[target]) return boxes[target];
      return target !== null || this.tagName === "A"
        ? new DOMRect(40, 60, 200, 40)
        : new DOMRect(0, 0, 0, 0);
    },
  );
}

describe("TourOverlay", () => {
  beforeEach(() => sizeElements());
  afterEach(() => {
    act(() => endTour());
    vi.restoreAllMocks();
  });

  it("stays out of the way until the tour is started", async () => {
    renderDashboard();
    await screen.findByRole("button", { name: "Take the tour" });
    expect(callout()).toBeNull();
  });

  it("opens at the first step and walks forward and back", async () => {
    const user = userEvent.setup();
    renderDashboard();
    act(() => startTour());

    expect(
      await screen.findByRole("dialog", { name: titleOf("health") }),
    ).toHaveTextContent(`1 of ${TOUR_STEPS.length}`);
    expect(screen.queryByRole("button", { name: "Back" })).toBeNull();

    await user.click(screen.getByRole("button", { name: "Next" }));
    expect(
      await screen.findByRole("dialog", { name: titleOf("ask") }),
    ).toHaveTextContent(`2 of ${TOUR_STEPS.length}`);

    await user.click(screen.getByRole("button", { name: "Back" }));
    expect(
      await screen.findByRole("dialog", { name: titleOf("health") }),
    ).toBeInTheDocument();
  });

  it("moves with the arrow keys, keeping focus on Next between steps on one page", async () => {
    const user = userEvent.setup();
    renderDashboard();
    act(() => startTour());
    await screen.findByRole("dialog", { name: titleOf("health") });

    expect(screen.getByRole("button", { name: "Next" })).toHaveFocus();
    await user.keyboard("{ArrowRight}");
    expect(
      await screen.findByRole("dialog", { name: titleOf("ask") }),
    ).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Next" })).toHaveFocus();
    await user.keyboard("{ArrowLeft}");
    expect(
      await screen.findByRole("dialog", { name: titleOf("health") }),
    ).toBeInTheDocument();
  });

  it("fades the callout out while the spotlight moves, and back in with the next step's words", async () => {
    const user = userEvent.setup();
    renderDashboard();
    act(() => startTour());
    const dialog = await screen.findByRole("dialog", {
      name: titleOf("health"),
    });
    await waitFor(() => expect(dialog).not.toHaveAttribute("data-hidden"));

    await user.click(screen.getByRole("button", { name: "Next" }));
    // Out of sight at once, and still saying what it said, so its words do
    // not change where they can be seen...
    expect(dialog).toHaveAttribute("data-hidden");
    expect(dialog).toHaveAccessibleName(titleOf("health"));
    // ...and back once the spotlight is on the next element.
    await waitFor(() => expect(dialog).not.toHaveAttribute("data-hidden"));
    expect(dialog).toHaveAccessibleName(titleOf("ask"));
  });

  it.each([
    {
      layout: "side by side",
      wide: true,
      order: ["pipeline", "insights-strip"],
    },
    { layout: "stacked", wide: false, order: ["insights-strip", "pipeline"] },
  ])(
    "takes Home's two columns in reading order when they are $layout",
    async ({ wide, order }) => {
      vi.spyOn(window, "matchMedia").mockImplementation(
        (query: string) =>
          ({
            matches: query === HOME_SIDE_BY_SIDE && wide,
            media: query,
          }) as MediaQueryList,
      );
      const user = userEvent.setup();
      renderDashboard();
      act(() => showTourStep(stepAt("ask")));
      await screen.findByRole("dialog", { name: titleOf("ask") });

      for (const target of order) {
        await user.click(screen.getByRole("button", { name: "Next" }));
        expect(
          await screen.findByRole("dialog", { name: titleOf(target) }),
        ).toBeInTheDocument();
      }
    },
  );

  it("opens the page each step is on", async () => {
    const router = renderDashboard();

    act(() => showTourStep(stepAt("insights-triage")));
    await waitFor(() =>
      expect(router.state.location.pathname).toBe("/insights"),
    );

    act(() => showTourStep(stepAt("recent-runs")));
    await waitFor(() => expect(router.state.location.pathname).toBe("/"));
  });

  it("opens the top insight and the latest finished investigation for the steps that need one", async () => {
    const router = renderDashboard();

    act(() => showTourStep(stepAt("insight-evidence")));
    await waitFor(() =>
      expect(router.state.location.pathname).toBe("/insights/ins-1"),
    );

    // The newer run is still going and has nothing to show yet.
    act(() => showTourStep(stepAt("run-header")));
    await waitFor(() =>
      expect(router.state.location.pathname).toBe("/investigations/run-done"),
    );
  });

  it("skips the steps a deployment has nothing for, in the direction it is going", async () => {
    const user = userEvent.setup();
    renderDashboard({ insights: false });

    act(() => showTourStep(stepAt("insights-triage")));
    await screen.findByRole("dialog", { name: titleOf("insights-triage") });

    // No insight to open, so both insight steps are passed over.
    await user.click(screen.getByRole("button", { name: "Next" }));
    expect(
      await screen.findByRole("dialog", { name: titleOf("run-header") }),
    ).toBeInTheDocument();

    // And on the way back, Back lands before them rather than bouncing forward.
    await user.click(screen.getByRole("button", { name: "Back" }));
    expect(
      await screen.findByRole("dialog", { name: titleOf("insights-triage") }),
    ).toBeInTheDocument();
  });

  // Changing pages without showing how left the reader no way to get back
  // there on their own.
  it("points at and clicks the link to the next page, the page's own over the sidebar's", async () => {
    const user = userEvent.setup();
    const router = renderDashboard({ links: true });

    act(() => showTourStep(stepAt("recent-runs")));
    await screen.findByRole("dialog", { name: titleOf("recent-runs") });
    await user.click(screen.getByRole("button", { name: "Next" }));

    // The sidebar has an "All" and Home a "View all" to the same page; the
    // tour leaves through the one on the page.
    expect(await screen.findByTestId("tour-pointer")).toHaveTextContent(
      "Click “View all”",
    );
    expect(callout()).toBeNull();

    await waitFor(
      () => expect(router.state.location.pathname).toBe("/insights"),
      SLOW,
    );
    expect(
      await screen.findByRole(
        "dialog",
        { name: titleOf("insights-triage") },
        SLOW,
      ),
    ).toBeInTheDocument();
    expect(screen.queryByTestId("tour-pointer")).toBeNull();
  });

  // Setting off from where the reader just clicked is what lets the eye
  // follow it to the link.
  it("sets the pointer off from the button that was pressed", async () => {
    const user = userEvent.setup();
    renderDashboard({ links: true });
    act(() => showTourStep(stepAt("recent-runs")));
    await screen.findByRole("dialog", { name: titleOf("recent-runs") });
    const next = screen.getByRole("button", { name: "Next" });
    // Its own box, not the prototype's stand-in, which a spy on the element
    // would reuse and so move every element with it.
    next.getBoundingClientRect = () => new DOMRect(500, 400, 60, 30);

    await user.click(next);
    // The arrow's tip starts on the middle of Next...
    const pointer = await screen.findByTestId("tour-pointer");
    const [x, y] = tipOf(pointer);
    expect(Math.abs(x - 530)).toBeLessThan(10);
    expect(Math.abs(y - 415)).toBeLessThan(10);
    // ...and comes to rest on the middle of the link.
    await waitFor(() => expect(tipOf(pointer)).toEqual([140, 80]), SLOW);
  });

  it("names an icon link by its label", async () => {
    renderDashboard({ links: true });

    act(() => showTourStep(stepAt("run-header")));
    expect(await screen.findByTestId("tour-pointer")).toHaveTextContent(
      "Click “Latest investigation”",
    );
  });

  it("changes pages without the pointer when motion is reduced", async () => {
    vi.spyOn(window, "matchMedia").mockImplementation(
      (query: string) =>
        ({ matches: query.includes("reduce"), media: query }) as MediaQueryList,
    );
    const router = renderDashboard({ links: true });

    act(() => showTourStep(stepAt("run-header")));
    await waitFor(() =>
      expect(router.state.location.pathname).toBe("/investigations/run-done"),
    );
    expect(screen.queryByTestId("tour-pointer")).toBeNull();
  });

  it("ends on Escape, on the close button, and on Done", async () => {
    const user = userEvent.setup();
    renderDashboard();

    act(() => startTour());
    await screen.findByRole("dialog");
    await user.keyboard("{Escape}");
    await waitFor(() => expect(callout()).toBeNull());

    act(() => startTour());
    await user.click(
      await screen.findByRole("button", { name: "End the tour" }),
    );
    await waitFor(() => expect(callout()).toBeNull());

    act(() => showTourStep(TOUR_STEPS.length - 1));
    await user.click(await screen.findByRole("button", { name: "Done" }));
    await waitFor(() => expect(callout()).toBeNull());
  });

  it("ends on Escape while the pointer is on its way", async () => {
    const user = userEvent.setup();
    const router = renderDashboard({ links: true });

    act(() => showTourStep(stepAt("run-header")));
    await screen.findByTestId("tour-pointer");
    await user.keyboard("{Escape}");

    await waitFor(() =>
      expect(screen.queryByTestId("tour-pointer")).toBeNull(),
    );
    // The click it was about to make is called off with it.
    await new Promise((resolve) => setTimeout(resolve, 1500));
    expect(router.state.location.pathname).toBe("/");
  });

  it("opens on load when the address asks for it", async () => {
    renderDashboard({ at: "/?tour" });
    expect(
      await screen.findByRole("dialog", { name: titleOf("health") }),
    ).toBeInTheDocument();
  });

  it("frames the step's element, and leaves the callout up when there is none", async () => {
    sizeElements({
      health: new DOMRect(100, 50, 400, 80),
      ask: new DOMRect(0, 0, 0, 0),
    });
    renderDashboard();

    act(() => startTour());
    const spotlight = await screen.findByTestId("tour-spotlight");
    await waitFor(() =>
      expect(spotlight).toHaveStyle({
        left: "94px",
        top: "44px",
        width: "412px",
        height: "92px",
      }),
    );

    // The ask box has no size, which is how an element that is not on screen
    // looks: the spotlight gives up on it after a moment, and the callout
    // opens anyway.
    act(() => showTourStep(stepAt("ask")));
    expect(
      await screen.findByRole("dialog", { name: titleOf("ask") }, SLOW),
    ).toBeInTheDocument();
    expect(screen.queryByTestId("tour-spotlight")).toBeNull();
  });

  it("glides the spotlight from one element to the next rather than jumping", async () => {
    sizeElements({
      health: new DOMRect(100, 50, 400, 80),
      ask: new DOMRect(100, 450, 400, 80),
    });
    renderDashboard();
    act(() => startTour());
    const spotlight = await screen.findByTestId("tour-spotlight");
    await waitFor(() => expect(spotlight).toHaveStyle({ top: "44px" }));

    const tops = new Set<number>();
    const sample = setInterval(
      () => tops.add(parseFloat(spotlight.style.top)),
      5,
    );
    act(() => showTourStep(stepAt("ask")));
    await waitFor(() => expect(spotlight).toHaveStyle({ top: "444px" }));
    clearInterval(sample);
    expect(
      [...tops].filter((top) => top > 44 && top < 444).length,
    ).toBeGreaterThan(3);
  });
});

/** Where the pointer's arrow tip is, as [x, y]: the arrow is drawn 4px up and
 *  left of it. */
function tipOf(pointer: HTMLElement): number[] {
  return (pointer.style.transform.match(/-?[\d.]+/g) ?? []).map(
    (value) => Number(value) + 4,
  );
}

// Where the callout finally lands is the popover's collision handling, which
// needs a layout this environment does not do, so where it asks to go is
// pinned here instead.
describe("placeCallout", () => {
  it("opens below an element of ordinary height", () => {
    const box = new DOMRect(300, 100, 900, 120);
    expect(placeCallout(box, 1440, 900)).toEqual({
      anchor: box,
      side: "bottom",
      align: "start",
    });
  });

  it("opens in the middle of the window when there is nothing to point at", () => {
    expect(placeCallout(null, 1440, 900)).toMatchObject({
      side: "bottom",
      align: "center",
    });
    expect(placeCallout(null, 1440, 900).anchor).toMatchObject({
      x: 720,
      y: 300,
    });
  });

  // Above or below an element as tall as the window there was no room at all,
  // and the callout ended up above the top of the window.
  it("opens beside an element taller than half the window, on a side with room", () => {
    expect(placeCallout(new DOMRect(0, 40, 260, 860), 1440, 900).side).toBe(
      "right",
    );
    expect(placeCallout(new DOMRect(1180, 40, 260, 860), 1440, 900).side).toBe(
      "left",
    );
  });

  // On a phone a tall section fills the width too, and beside it the callout
  // was pushed off the right edge, Next and all.
  it("opens at the bottom of the window when a tall element leaves no room beside it", () => {
    const placement = placeCallout(new DOMRect(16, 180, 358, 520), 390, 844);
    expect(placement).toMatchObject({ side: "top", align: "center" });
    expect(placement.anchor).toMatchObject({ x: 195, y: 844 });
  });
});
