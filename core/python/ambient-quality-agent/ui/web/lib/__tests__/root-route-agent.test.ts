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

import { describe, expect, it, vi } from "vitest";
import { createMemoryHistory, createRouter } from "@tanstack/react-router";
import { routeTree } from "@/src/routeTree.gen";

/** The app's own route tree, opened at `url`. */
function openAt(url: string) {
  return createRouter({
    routeTree,
    history: createMemoryHistory({ initialEntries: [url] }),
  });
}

describe("the root route", () => {
  it("keeps the selected agent on a link that does not name one", () => {
    const router = openAt("/insights?agent=acme-prod__support&status=new");

    const next = router.buildLocation({
      to: "/investigations/$runId",
      params: { runId: "r-1" },
    });

    expect(next.href).toBe("/investigations/r-1?agent=acme-prod__support");
  });

  it("keeps it when a navigation replaces the whole search", () => {
    const router = openAt("/c?agent=acme-prod__support&id=ctx-1");

    const next = router.buildLocation({ to: "/c", search: {} });

    expect(next.href).toBe("/c?agent=acme-prod__support");
  });

  it("keeps it through a navigation, which validates the search", async () => {
    // `navigate` runs the root's `validateSearch` inside the middleware that
    // keeps the agent; `buildLocation` alone does not.
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => new Response("{}", { status: 200 })),
    );
    const router = openAt("/c?agent=acme-prod__support&id=ctx-1");
    await router.load();

    await router.navigate({ to: "/c", search: {} });

    expect(router.state.location.href).toBe("/c?agent=acme-prod__support");
    vi.unstubAllGlobals();
  });

  it("adds nothing for the default agent", () => {
    const router = openAt("/insights");

    expect(router.buildLocation({ to: "/config" }).href).toBe("/config");
  });
});
