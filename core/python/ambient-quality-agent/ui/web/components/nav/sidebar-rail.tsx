"use client";
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

import { useCallback, useEffect, useRef, useState } from "react";
import type { ImperativePanelHandle } from "react-resizable-panels";

import { ChatListSidebar } from "@/components/chat/chat-list-sidebar";
import {
  ResizableHandle,
  ResizablePanel,
  ResizablePanelGroup,
} from "@/components/ui/resizable";
import {
  Sheet,
  SheetContent,
  SheetHeader,
  SheetTitle,
} from "@/components/ui/sheet";
import { publishRailState, retractRailState } from "@/lib/rail-state";
import { useMediaQuery } from "@/lib/use-media-query";

type SidebarProps = React.ComponentProps<typeof ChatListSidebar>;

/** Focuses the tab bar's "Show chats" button, if it is rendered. Found by the
 * `data-rail-toggle` marker because the tab bar is a sibling this component
 * cannot reach. */
function focusShowChats(): void {
  document.querySelector<HTMLElement>("[data-rail-toggle]")?.focus();
}

/** The left rail, resizable and collapsible, wrapping a route's content.
 *
 * Adapted from horizon's chat shell, which makes its sidebar a
 * `ResizablePanel` (defaultSize 18, min 14, max 30) inside an `autoSaveId`
 * group. Two changes:
 *
 *  - It lives here rather than in the chat shell, because AQuA shows the same
 *    rail on every route. Two copies drifted to different widths once already.
 *  - `collapsible`, which horizon's left panel is not: the rail is the only
 *    navigation, so hiding it needs a control that puts it back. That control
 *    is in the tab bar, reached through `lib/rail-state`.
 *
 * `autoSaveId` persists the width, so the group is declared once here and the
 * routes cannot disagree about it.
 *
 * Below md the rail becomes a left drawer, opened from the same tab-bar
 * control: a percentage width there is 57-70px, too narrow for any label.
 * `showRail={false}` opts a route out of both, for the chat route, which
 * brings its own drawer.
 */
export function SidebarRail({
  children,
  showRail = true,
  ...sidebar
}: SidebarProps & { children: React.ReactNode; showRail?: boolean }) {
  const rail = useRef<ImperativePanelHandle>(null);
  const railContent = useRef<HTMLDivElement>(null);
  const [collapsed, setCollapsed] = useState(false);
  // Set by the buttons, not by a drag: only they take away the element that
  // has focus (the "Hide chats" button, or the tab bar's "Show chats").
  const focusAfterToggle = useRef(false);
  const isDesktop = useMediaQuery("(min-width: 768px)");
  const [drawerOpen, setDrawerOpen] = useState(false);
  const openDrawer = useCallback(() => setDrawerOpen(true), []);
  const closeDrawer = useCallback(() => setDrawerOpen(false), []);

  // Direction comes from this component's own flag rather than the panel's
  // `isCollapsed()`, which asserts on a measured size and throws before the
  // first layout. `onCollapse`/`onExpand` keep the flag honest when the user
  // drags the handle instead of pressing the button.
  const toggle = useCallback(() => {
    const next = !collapsed;
    focusAfterToggle.current = true;
    setCollapsed(next);
    if (next) rail.current?.collapse();
    else rail.current?.expand();
  }, [collapsed]);

  // Collapsing to zero leaves nothing to drag, so the only way back is a
  // control outside the panel. It lives in the tab bar, which this component
  // cannot reach by props -- hence the store.
  // Below md there is no rail to collapse, so the tab bar's "Show chats"
  // always shows and opens the drawer.
  useEffect(() => {
    if (!showRail) return;
    const published = isDesktop ? toggle : openDrawer;
    publishRailState({
      collapsed: isDesktop ? collapsed : true,
      toggle: published,
    });
    return () => retractRailState(published);
  }, [collapsed, isDesktop, openDrawer, showRail, toggle]);

  // Widening the window past md swaps the drawer for the rail; narrowing it
  // again must not bring back a drawer nobody asked to open.
  useEffect(() => {
    if (isDesktop) setDrawerOpen(false);
  }, [isDesktop]);

  // A collapsed panel is about 1px wide but its contents can still take focus,
  // so they leave the tab order and the accessibility tree with it. React 18
  // does not know `inert`, hence the attribute is set by hand, again on the
  // new rail that widening the window past md mounts.
  // biome-ignore lint/correctness/useExhaustiveDependencies(isDesktop): crossing the md breakpoint mounts a new rail that needs the inert attribute reapplied.
  useEffect(() => {
    const content = railContent.current;
    if (!content) return;
    content.toggleAttribute("inert", collapsed);
    if (!focusAfterToggle.current) return;
    focusAfterToggle.current = false;
    // The control that had focus is now inert or unmounted, which leaves
    // focus nowhere. Hand it to the control that undoes the toggle: the tab
    // bar's "Show chats", which only exists once the tab bar has re-rendered
    // from the state published above, or the rail's own "Hide chats".
    const frame = requestAnimationFrame(() => {
      if (collapsed) focusShowChats();
      else
        content
          .querySelector<HTMLElement>('button[aria-label="Hide chats"]')
          ?.focus();
    });
    return () => cancelAnimationFrame(frame);
  }, [collapsed, isDesktop]);

  if (!showRail) return <>{children}</>;

  // One tree at every width, with the rail's pieces in fixed slots: were the
  // root to change between breakpoints, crossing md would remount `children`
  // and lose the route's own state, such as a half-typed question on Home.
  return (
    <ResizablePanelGroup
      direction="horizontal"
      autoSaveId="aqua.rail"
      className="h-full min-h-0"
    >
      {isDesktop && (
        <ResizablePanel
          id="rail"
          order={1}
          ref={rail}
          defaultSize={18}
          minSize={12}
          maxSize={30}
          collapsible
          collapsedSize={0}
          onCollapse={() => setCollapsed(true)}
          onExpand={() => setCollapsed(false)}
          className="flex min-h-0 flex-col"
        >
          <div ref={railContent} className="flex min-h-0 flex-1 flex-col">
            <ChatListSidebar {...sidebar} onCollapse={toggle} />
          </div>
        </ResizablePanel>
      )}
      {/* Collapsed, the handle is a 1px focus stop at the window edge, and
          the tab bar's "Show chats" is the way back. */}
      {isDesktop && (
        <ResizableHandle withHandle tabIndex={collapsed ? -1 : 0} />
      )}
      <ResizablePanel
        id="body"
        order={2}
        minSize={40}
        className="flex min-h-0 min-w-0 flex-col"
      >
        {!isDesktop && (
          <Sheet open={drawerOpen} onOpenChange={setDrawerOpen}>
            <SheetContent
              side="left"
              className="w-72 p-0"
              // Top insights and Recent investigations rows are plain links that
              // never call onNavigate; following one must close the drawer too.
              onClick={(e) => {
                if ((e.target as Element).closest("a[href]"))
                  setDrawerOpen(false);
              }}
              // No SheetTrigger to return focus to: it is the tab bar's button.
              onCloseAutoFocus={(e) => {
                e.preventDefault();
                focusShowChats();
              }}
            >
              <SheetHeader className="sr-only">
                <SheetTitle>Chats</SheetTitle>
              </SheetHeader>
              <ChatListSidebar
                {...sidebar}
                onNavigate={closeDrawer}
                reserveHeaderRight
              />
            </SheetContent>
          </Sheet>
        )}
        {children}
      </ResizablePanel>
    </ResizablePanelGroup>
  );
}
