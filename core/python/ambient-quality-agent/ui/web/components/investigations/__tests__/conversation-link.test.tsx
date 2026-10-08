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
 * Three facts, not two. A run started from a chat, a run the ambient loop
 * started and so never had one, and a run a person asked for whose chat went
 * unrecorded. The middle case is most of them, and reporting it as "no chat
 * recorded" described a healthy deployment as if something were missing.
 */
import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";
import {
  RouterProvider,
  createRootRoute,
  createRoute,
  createRouter,
  createMemoryHistory,
} from "@tanstack/react-router";

import { ConversationLink } from "../conversation-link";

function renderAt(contextId?: string | null, triggerType?: string | null) {
  const root = createRootRoute();
  const index = createRoute({
    getParentRoute: () => root,
    path: "/",
    component: () => (
      <ConversationLink contextId={contextId} triggerType={triggerType} />
    ),
  });
  const chat = createRoute({
    getParentRoute: () => root,
    path: "/c",
    validateSearch: (s: Record<string, unknown>) => ({ id: s.id as string }),
    component: () => <div>chat</div>,
  });
  const router = createRouter({
    routeTree: root.addChildren([index, chat]),
    history: createMemoryHistory({ initialEntries: ["/"] }),
  });
  return render(<RouterProvider router={router as never} />);
}

describe("ConversationLink", () => {
  it("links to the chat that started the run", async () => {
    renderAt("ctx-abc");

    const link = await screen.findByRole("link");
    expect(link.getAttribute("href")).toContain("ctx-abc");
  });

  it("calls a scheduled run ambient rather than reporting a missing chat", async () => {
    renderAt(null, "scheduled");

    expect(await screen.findByText("ambient")).toBeTruthy();
    expect(screen.queryByText(/No chat recorded/i)).toBeNull();
    expect(screen.queryByRole("link")).toBeNull();
  });

  it("says the same of the other ambient trigger types", async () => {
    renderAt(null, "task_fire");
    expect(await screen.findByText("ambient")).toBeTruthy();
  });

  it("still reports a manual run with no chat as a gap, because it is one", async () => {
    // A person asked for this sweep and we did not record where from. Calling
    // that "ambient" would be false.
    renderAt(null, "manual");

    const el = await screen.findByText(/No chat recorded/i);
    expect(el).toBeTruthy();
    expect(el.getAttribute("title")).toContain(
      "This investigation was not started from a chat",
    );
    expect(screen.queryByText("ambient")).toBeNull();
  });

  it("does not guess when the record carries no trigger", async () => {
    renderAt(null, "");

    expect(await screen.findByText(/No chat recorded/i)).toBeTruthy();
  });

  it("treats an empty context id as no chat, not as a link to nowhere", async () => {
    renderAt("", "scheduled");

    expect(screen.queryByRole("link")).toBeNull();
    expect(await screen.findByText("ambient")).toBeTruthy();
  });
});
