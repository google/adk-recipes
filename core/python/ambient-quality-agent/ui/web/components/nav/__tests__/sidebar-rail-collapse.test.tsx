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
 * What a collapsed rail does to the keyboard.
 *
 * The panels are faked: the real `collapse()` asserts on a measured size that
 * happy-dom cannot provide (see sidebar-rail.test.tsx). The fake reports the
 * collapse the way the library does, through `onCollapse`/`onExpand`, which is
 * all the rail relies on. The real collapse is covered in
 * `e2e/sidebar.spec.mjs`.
 */
import { describe, expect, it, vi, beforeEach } from "vitest";
import { act, render, screen, fireEvent } from "@testing-library/react";
import type React from "react";
import { forwardRef, useImperativeHandle } from "react";

vi.mock("@/components/chat/chat-list-sidebar", () => ({
  ChatListSidebar: ({ onCollapse }: { onCollapse?: () => void }) => (
    <nav aria-label="Sessions">
      {/* Labelled like the real one: the rail finds it by that label. */}
      <button type="button" aria-label="Hide chats" onClick={onCollapse}>
        <svg />
      </button>
    </nav>
  ),
}));

vi.mock("@/components/ui/resizable", () => {
  type FakePanelProps = React.HTMLAttributes<HTMLDivElement> & {
    onCollapse?: () => void;
    onExpand?: () => void;
  };
  const panelOnlyProps = [
    "order",
    "defaultSize",
    "minSize",
    "maxSize",
    "collapsible",
    "collapsedSize",
  ];
  return {
    ResizablePanelGroup: ({ children }: { children: React.ReactNode }) => (
      <div>{children}</div>
    ),
    ResizablePanel: forwardRef(function FakePanel(
      { onCollapse, onExpand, children, ...rest }: FakePanelProps,
      ref,
    ) {
      useImperativeHandle(ref, () => ({
        collapse: () => onCollapse?.(),
        expand: () => onExpand?.(),
      }));
      const domProps = Object.fromEntries(
        Object.entries(rest).filter(([k]) => !panelOnlyProps.includes(k)),
      );
      return <div {...domProps}>{children}</div>;
    }),
    ResizableHandle: ({ tabIndex }: { tabIndex?: number }) => (
      // biome-ignore lint/a11y/useSemanticElements: mirrors the focusable div that react-resizable-panels renders as its handle.
      // biome-ignore lint/a11y/useAriaPropsForRole: the stub carries only the role and tabIndex that the collapse tests assert on.
      <div role="separator" tabIndex={tabIndex ?? 0} />
    ),
  };
});

import { useRailState } from "@/lib/rail-state";
import { SidebarRail } from "../sidebar-rail";

/** Stands in for the tab bar's "Show chats" button, which renders only while
 * the rail reports itself collapsed. */
function ShowChatsProbe({ label = "Show chats" }: { label?: string }) {
  const { collapsed, toggle } = useRailState();
  if (!collapsed || !toggle) return null;
  return (
    <button
      type="button"
      aria-label={label}
      data-rail-toggle=""
      onClick={toggle}
    >
      show
    </button>
  );
}

/** Lets the focus hand-off, which waits for the tab bar to re-render, run. */
async function nextFrame() {
  await act(
    () =>
      new Promise<void>((resolve) => requestAnimationFrame(() => resolve())),
  );
}

let desktop = true;
const mediaListeners = new Set<() => void>();

/** Answers `(min-width: 768px)`, and tells the listening queries when the
 *  answer changes, as a resize across md does. */
function setDesktop(next: boolean) {
  desktop = next;
  act(() => {
    for (const listener of mediaListeners) listener();
  });
}

beforeEach(() => {
  desktop = true;
  window.matchMedia = ((query: string) => ({
    get matches() {
      return desktop;
    },
    media: query,
    addEventListener(_: string, listener: () => void) {
      mediaListeners.add(listener);
    },
    removeEventListener(_: string, listener: () => void) {
      mediaListeners.delete(listener);
    },
  })) as unknown as typeof window.matchMedia;
});

function renderRail() {
  return render(
    <SidebarRail
      activeContextId={null}
      sessions={[]}
      sessionsLoading={false}
      refreshSessions={async () => undefined}
    >
      <div>route body</div>
    </SidebarRail>,
  );
}

describe("SidebarRail collapsed", () => {
  it("takes a collapsed rail out of the tab order and the accessibility tree", () => {
    renderRail();
    const nav = screen.getByRole("navigation", { name: /sessions/i });
    expect(nav.closest("[inert]")).toBeNull();

    // A collapsed panel is about 1px wide, but everything in it could still
    // take focus: 35 of 40 Tab presses landed there.
    fireEvent.click(screen.getByRole("button", { name: /hide chats/i }));

    expect(nav.closest("[inert]")).not.toBeNull();
  });

  it("keeps a collapsed rail inert after the window narrows past md and back", () => {
    renderRail();
    fireEvent.click(screen.getByRole("button", { name: /hide chats/i }));

    setDesktop(false);
    setDesktop(true);

    const nav = screen.getByRole("navigation", {
      name: /sessions/i,
      hidden: true,
    });
    expect(nav.closest("[inert]")).not.toBeNull();
  });

  it("hands focus to the tab bar's way back when collapsing under it", async () => {
    render(<ShowChatsProbe />);
    renderRail();
    const hide = screen.getByRole("button", { name: /hide chats/i });
    hide.focus();

    fireEvent.click(hide);
    await nextFrame();

    // Otherwise focus is inside an inert subtree, i.e. nowhere.
    expect(document.activeElement).toBe(
      screen.getByRole("button", { name: /show chats/i }),
    );
  });

  it("finds the tab bar's way back by its marker, not by its label", async () => {
    render(<ShowChatsProbe label="Open the sidebar" />);
    renderRail();
    const hide = screen.getByRole("button", { name: /hide chats/i });
    hide.focus();

    fireEvent.click(hide);
    await nextFrame();

    expect(document.activeElement).toBe(
      screen.getByRole("button", { name: "Open the sidebar" }),
    );
  });

  it("gives the rail back to the keyboard when it reopens", async () => {
    render(<ShowChatsProbe />);
    renderRail();
    const nav = screen.getByRole("navigation", { name: /sessions/i });
    fireEvent.click(screen.getByRole("button", { name: /hide chats/i }));
    await nextFrame();

    const show = screen.getByRole("button", { name: /show chats/i });
    show.focus();
    fireEvent.click(show);
    await nextFrame();

    expect(nav.closest("[inert]")).toBeNull();
    // The button that had focus is gone, so focus lands on its counterpart.
    expect(document.activeElement).toBe(
      screen.getByRole("button", { name: /hide chats/i }),
    );
  });

  it("takes the resize handle out of the tab order while collapsed", () => {
    renderRail();
    const handle = screen.getByRole("separator");

    // At zero width the handle is a 1px focus stop at the window edge, and
    // the tab bar's "Show chats" already brings the rail back.
    fireEvent.click(screen.getByRole("button", { name: /hide chats/i }));
    expect(handle.tabIndex).toBe(-1);

    fireEvent.click(
      screen.getByRole("button", { name: /hide chats/i, hidden: true }),
    );
    expect(handle.tabIndex).toBe(0);
  });
});
