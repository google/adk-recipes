/**
 * Copyright 2026 Google LLC
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 *
 * Unless required by applicable law or agreed to in writing, software
 * distributed under the License is distributed on an "AS IS" BASIS,
 * WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 * See the License for the specific language governing permissions and
 * limitations under the License.
 */

/**
 * The rail is the only navigation, so hiding it needs a way back.
 *
 * `ChatListSidebar` is mocked: this covers the rail's own mechanics, not the
 * sidebar's contents, which have their own tests and would drag the router
 * and query client in here.
 *
 * The collapse round trip is NOT covered here. `react-resizable-panels`
 * asserts on a measured panel size before it will collapse, and happy-dom has
 * no layout, so `collapse()` throws whatever the harness reports. Stubbing
 * `ResizeObserver` and `getBoundingClientRect` was not enough. It is covered
 * in `e2e/sidebar.spec.mjs`, which runs in a browser that has layout.
 */
import { describe, expect, it, vi, beforeEach } from "vitest";
import {
  act,
  render,
  screen,
  fireEvent,
  waitFor,
  within,
} from "@testing-library/react";
import React from "react";

/** Every `onNavigate` the sidebar was rendered with, oldest first. */
const navigateProps = vi.hoisted(() => [] as unknown[]);

vi.mock("@/components/chat/chat-list-sidebar", () => ({
  ChatListSidebar: ({
    onCollapse,
    onNavigate,
  }: {
    onCollapse?: () => void;
    onNavigate?: () => void;
  }) => {
    navigateProps.push(onNavigate);
    return (
      <nav aria-label="Sessions">
        {onCollapse && (
          <button type="button" onClick={onCollapse}>
            Hide chats
          </button>
        )}
        <button type="button" onClick={onNavigate}>
          Open a chat
        </button>
        <a href="/insights/1" onClick={(e) => e.preventDefault()}>
          An insight
        </a>
      </nav>
    );
  },
}));

import { useRailState } from "@/lib/rail-state";
import { SidebarRail } from "../sidebar-rail";

/** Reads what the rail published for the tab bar, which lives outside it. */
function RailProbe() {
  const { collapsed, toggle } = useRailState();
  return (
    <div data-testid="probe">
      {toggle ? (collapsed ? "collapsed" : "expanded") : "no rail"}
    </div>
  );
}

/** Stands in for the tab bar's "Show chats" button, which renders only while
 * the rail reports itself collapsed. */
function ShowChatsProbe() {
  const { collapsed, toggle } = useRailState();
  if (!collapsed || !toggle) return null;
  return (
    <button
      type="button"
      aria-label="Show chats"
      data-rail-toggle=""
      onClick={toggle}
    >
      show
    </button>
  );
}

let viewportWidth = 1440;
const mediaListeners = new Set<() => void>();

/** Answers `(min-width: 768px)` as a window of the given width would, and
 *  tells the queries already listening, as a resize does. */
function setViewportWidth(width: number) {
  viewportWidth = width;
  window.matchMedia = ((query: string) => ({
    get matches() {
      return query.includes("min-width: 768px") ? viewportWidth >= 768 : false;
    },
    media: query,
    addEventListener(_: string, listener: () => void) {
      mediaListeners.add(listener);
    },
    removeEventListener(_: string, listener: () => void) {
      mediaListeners.delete(listener);
    },
  })) as unknown as typeof window.matchMedia;
  act(() => {
    for (const listener of mediaListeners) listener();
  });
}

beforeEach(() => {
  window.localStorage?.clear();
  setViewportWidth(1440);
  // happy-dom has no layout, and react-resizable-panels asserts on a measured
  // size before it will collapse. Report one: a stub that never fires leaves
  // the panels sizeless and every imperative call throws.
  globalThis.ResizeObserver = class {
    constructor(private cb: ResizeObserverCallback) {}
    observe(el: Element) {
      this.cb(
        [{ target: el, contentRect: { width: 1000, height: 800 } }] as never,
        this as never,
      );
    }
    unobserve() {}
    disconnect() {}
  } as unknown as typeof ResizeObserver;
  Element.prototype.getBoundingClientRect = () =>
    ({
      width: 1000,
      height: 800,
      top: 0,
      left: 0,
      right: 1000,
      bottom: 800,
    }) as DOMRect;
});

function renderRail(showRail?: boolean) {
  return render(
    <SidebarRail
      activeContextId={null}
      sessions={[]}
      sessionsLoading={false}
      refreshSessions={async () => undefined}
      {...(showRail === undefined ? {} : { showRail })}
    >
      <div>route body</div>
    </SidebarRail>,
  );
}

describe("SidebarRail", () => {
  it("renders the rail beside the route body", () => {
    renderRail();

    expect(screen.getByRole("navigation", { name: /sessions/i })).toBeTruthy();
    expect(screen.getByText("route body")).toBeTruthy();
  });

  it("is resizable rather than a fixed width", () => {
    renderRail();

    // The drag handle is what makes the width the user's choice.
    expect(document.querySelector('[role="separator"]')).not.toBeNull();
  });

  it("hands the sidebar a way to ask for collapse", () => {
    renderRail();

    // The button lives in the sidebar and the state lives in the rail, so the
    // wiring between them is what can break silently.
    expect(screen.getByRole("button", { name: /hide chats/i })).toBeTruthy();
  });

  it("publishes its toggle so the tab bar can offer the way back", () => {
    // The tab bar renders the "show chats" control and is a sibling of this
    // component, so the store is the only wiring between them.
    render(<RailProbe />);
    const rail = renderRail();
    expect(screen.getByTestId("probe").textContent).toBe("expanded");

    rail.unmount();
    expect(screen.getByTestId("probe").textContent).toBe("no rail");
  });

  it("drops the rail entirely when a route opts out", () => {
    render(<RailProbe />);
    renderRail(false);

    // The chat route hides it below md and shows a drawer instead.
    expect(screen.queryByRole("navigation", { name: /sessions/i })).toBeNull();
    expect(screen.getByText("route body")).toBeTruthy();
    // Nothing to collapse, so the tab bar must not offer to bring it back.
    expect(screen.getByTestId("probe").textContent).toBe("no rail");
  });

  it("persists the layout so the width survives navigation", () => {
    renderRail();

    const group = document.querySelector("[data-panel-group]");
    expect(group).not.toBeNull();
    expect(group?.getAttribute("data-panel-group-id")).toBeTruthy();
  });

  it("keeps the route's own state when the window crosses md", () => {
    function Draft() {
      const [text, setText] = React.useState("");
      return (
        <input
          aria-label="draft"
          value={text}
          onChange={(e) => setText(e.target.value)}
        />
      );
    }
    render(
      <SidebarRail
        activeContextId={null}
        sessions={[]}
        sessionsLoading={false}
        refreshSessions={async () => undefined}
      >
        <Draft />
      </SidebarRail>,
    );
    fireEvent.change(screen.getByLabelText("draft"), {
      target: { value: "half a question" },
    });

    // A tablet rotating, a window dragged narrower, devtools docking.
    setViewportWidth(700);
    setViewportWidth(1200);

    expect((screen.getByLabelText("draft") as HTMLInputElement).value).toBe(
      "half a question",
    );
  });

  describe("below md", () => {
    beforeEach(() => setViewportWidth(390));

    it("offers the sidebar as a drawer instead of a squeezed rail", () => {
      render(<ShowChatsProbe />);
      renderRail();

      // At 320-390px an 18% rail is 57-70px: labels cut to a letter and the
      // page scrolling both ways.
      expect(
        screen.queryByRole("navigation", { name: /sessions/i }),
      ).toBeNull();
      expect(document.querySelector('[role="separator"]')).toBeNull();
      expect(screen.getByText("route body")).toBeTruthy();

      fireEvent.click(screen.getByRole("button", { name: /show chats/i }));

      const drawer = screen.getByRole("dialog");
      expect(
        within(drawer).getByRole("navigation", { name: /sessions/i }),
      ).toBeTruthy();
      // The Sheet has its own close button; a "Hide chats" in there would be
      // a second way to do the same thing.
      expect(
        within(drawer).queryByRole("button", { name: /hide chats/i }),
      ).toBeNull();
    });

    it("returns focus to the tab bar's control when the drawer closes", async () => {
      render(<ShowChatsProbe />);
      renderRail();
      const show = screen.getByRole("button", { name: /show chats/i });
      show.focus();
      fireEvent.click(show);

      // The Sheet has no SheetTrigger to hand focus back to, so without help
      // it falls to the body.
      fireEvent.click(
        within(screen.getByRole("dialog")).getByRole("button", {
          name: /close/i,
        }),
      );

      await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
      expect(document.activeElement).toBe(show);
    });

    it("closes the drawer when the sidebar navigates", () => {
      render(<ShowChatsProbe />);
      renderRail();

      fireEvent.click(screen.getByRole("button", { name: /show chats/i }));
      fireEvent.click(screen.getByRole("button", { name: /open a chat/i }));

      expect(screen.queryByRole("dialog")).toBeNull();
    });

    it("hands the drawer's sidebar the same close callback on every render", () => {
      render(<ShowChatsProbe />);
      const view = renderRail();
      fireEvent.click(screen.getByRole("button", { name: /show chats/i }));
      navigateProps.length = 0;

      // The real sidebar is memoised; a fresh callback would re-render the
      // whole list each time the rail does.
      for (const body of ["another body", "a third body"]) {
        view.rerender(
          <SidebarRail
            activeContextId={null}
            sessions={[]}
            sessionsLoading={false}
            refreshSessions={async () => undefined}
          >
            <div>{body}</div>
          </SidebarRail>,
        );
      }

      expect(navigateProps.length).toBeGreaterThan(1);
      expect(new Set(navigateProps).size).toBe(1);
    });

    it("closes the drawer when a link inside it is followed", () => {
      render(<ShowChatsProbe />);
      renderRail();

      // Rows in Top insights and Recent investigations are plain links that
      // never call onNavigate.
      fireEvent.click(screen.getByRole("button", { name: /show chats/i }));
      fireEvent.click(screen.getByRole("link", { name: /an insight/i }));

      expect(screen.queryByRole("dialog")).toBeNull();
    });

    it("leaves a route that opts out with neither rail nor drawer", () => {
      render(<RailProbe />);
      renderRail(false);

      // The chat route brings its own drawer; a second one would stack.
      expect(screen.getByTestId("probe").textContent).toBe("no rail");
      expect(screen.queryByRole("dialog")).toBeNull();
    });
  });
});
