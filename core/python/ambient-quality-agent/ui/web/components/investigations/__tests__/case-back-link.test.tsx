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
 * The case view is reached two ways: from a run, and from a chat preview whose
 * run segment is a placeholder. Sending the second one back to
 * `/investigations/preview` walks the user into an error page with no route
 * back to the conversation they came from.
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

import { CaseBackLink, PREVIEW_RUN_SEGMENT } from "../case-back-link";

function renderFor(runId: string) {
  const root = createRootRoute();
  const index = createRoute({
    getParentRoute: () => root,
    path: "/",
    component: () => <CaseBackLink runId={runId} />,
  });
  const chat = createRoute({
    getParentRoute: () => root,
    path: "/c",
    validateSearch: (s: Record<string, unknown>) => ({
      id: s.id as string | undefined,
    }),
    component: () => <div>chat</div>,
  });
  const run = createRoute({
    getParentRoute: () => root,
    path: "/investigations/$runId",
    component: () => <div>run</div>,
  });
  const router = createRouter({
    routeTree: root.addChildren([index, chat, run]),
    history: createMemoryHistory({ initialEntries: ["/"] }),
  });
  return render(<RouterProvider router={router as never} />);
}

describe("CaseBackLink", () => {
  it("sends a preview back to the chat it was opened from", async () => {
    renderFor(PREVIEW_RUN_SEGMENT);

    const link = await screen.findByRole("link");
    // No `?id=`: the chat shell restores the last conversation itself.
    expect(link.getAttribute("href")).toBe("/c");
    expect(link.textContent).toContain("Back to chat");
  });

  it("keeps the placeholder in step with the server", () => {
    expect(PREVIEW_RUN_SEGMENT).toBe("preview");
  });

  it("sends a real case back to its run", async () => {
    renderFor("0f3a9c21-5b6d-4e77-9a10-1c2d3e4f5a6b");

    const link = await screen.findByRole("link");
    expect(link.getAttribute("href")).toBe(
      "/investigations/0f3a9c21-5b6d-4e77-9a10-1c2d3e4f5a6b",
    );
    expect(link.textContent).toContain("Run 0f3a9c21");
  });
});
