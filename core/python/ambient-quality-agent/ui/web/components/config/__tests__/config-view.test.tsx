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

// biome-ignore-all lint/style/noNonNullAssertion: a missing value fails the test either way; the assertion only narrows the type.

import { afterEach, describe, expect, it, vi } from "vitest";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { ConfigView } from "../config-view";

/**
 * Renders the page against a fetch that answers the goal read with `goal`,
 * every other read with an empty object, and counts the reads of each path.
 *
 * Args:
 *   goal: Supplies the goal text for each read of `/api/goal`.
 *
 * Returns:
 *   The number of reads of each path, updated as the page reads.
 */
function renderConfig(goal: () => string) {
  const reads = new Map<string, number>();
  vi.stubGlobal("fetch", (async (input: RequestInfo | URL) => {
    const path = new URL(String(input), "http://x").pathname;
    reads.set(path, (reads.get(path) ?? 0) + 1);
    const body = path === "/api/goal" ? { goal: goal(), available: true } : {};
    return new Response(JSON.stringify(body), { status: 200 });
  }) as typeof fetch);
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  render(
    <QueryClientProvider client={queryClient}>
      <ConfigView />
    </QueryClientProvider>,
  );
  return reads;
}

async function clickRefresh() {
  const header = (
    await screen.findByRole("heading", { level: 1, name: "Configuration" })
  ).closest("header")!;
  await userEvent.click(
    within(header).getByRole("button", { name: "Refresh the data" }),
  );
}

describe("ConfigView", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("re-reads every card from the refresh button in its header", async () => {
    let goal = "Handle billing tickets.";
    const reads = renderConfig(() => goal);
    await screen.findByDisplayValue("Handle billing tickets.");
    await vi.waitFor(() => expect(reads.get("/api/schedule")).toBe(1));
    goal = "Handle refund tickets.";

    await clickRefresh();

    expect(
      await screen.findByDisplayValue("Handle refund tickets."),
    ).toBeInTheDocument();
    for (const path of ["/api/schedule", "/api/source", "/api/memories"]) {
      await vi.waitFor(() => expect(reads.get(path)).toBe(2));
    }
  });

  it("keeps an unsaved goal edit through a refresh", async () => {
    const reads = renderConfig(() => "Handle billing tickets.");
    const box = await screen.findByDisplayValue("Handle billing tickets.");
    await userEvent.clear(box);
    await userEvent.type(box, "A draft.");

    await clickRefresh();

    await vi.waitFor(() => expect(reads.get("/api/goal")).toBe(2));
    expect(screen.getByDisplayValue("A draft.")).toBeInTheDocument();
  });
});
