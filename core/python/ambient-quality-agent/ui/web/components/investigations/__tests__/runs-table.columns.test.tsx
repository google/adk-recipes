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
 * The runs table's insight columns are the counts a run records, not whatever
 * the funnel's Insights stage draws. The stage can grow a segment no run
 * counts -- resolved insights are one -- and a column derived from it would
 * read zero on every row without anything flagging it.
 */
import {
  RouterProvider,
  createMemoryHistory,
  createRootRoute,
  createRoute,
  createRouter,
} from "@tanstack/react-router";
import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { counters } from "@/lib/__fixtures__/counters";
import type { Run } from "@/lib/aqua-api";
import { RunsTable } from "../runs-table";

vi.mock("@/lib/funnel", async (importOriginal) => {
  const funnel = await importOriginal<typeof import("@/lib/funnel")>();
  return {
    ...funnel,
    STAGES: funnel.STAGES.map((stage) =>
      stage.id === "insights"
        ? {
            ...stage,
            segments: [
              ...stage.segments,
              {
                key: "insights_resolved",
                label: "resolved",
                tone: "bg-emerald-400",
              },
            ],
          }
        : stage,
    ),
  };
});

const RUN: Run = {
  run_id: "abcdef1234",
  observed_agent_name: "travel_desk_agent",
  created_at: "2026-09-14T06:00:20Z",
  finished_at: "2026-09-14T06:04:00Z",
  status: "done",
  elapsed_seconds: 12,
  metrics_passed: 0,
  metrics_failed: 0,
  metrics_errored: 0,
  counters: counters({
    traces_scanned: 4,
    traces_eval_passed: 4,
    insights_recurring: 2,
  }),
  error: null,
};

describe("the runs table's insight columns", () => {
  it("stay the counts a run records when the funnel's Insights stage grows", async () => {
    const root = createRootRoute();
    const router = createRouter({
      routeTree: root.addChildren([
        createRoute({
          getParentRoute: () => root,
          path: "/",
          component: () => <RunsTable runs={[RUN]} />,
        }),
        createRoute({
          getParentRoute: () => root,
          path: "/investigations/$runId",
        }),
      ]),
      history: createMemoryHistory({ initialEntries: ["/"] }),
    });
    // biome-ignore lint/suspicious/noExplicitAny: the test router is not the app's registered router type.
    render(<RouterProvider router={router as any} />);

    expect(
      await screen.findByRole("columnheader", { name: "Recurring" }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("columnheader", { name: "New" }),
    ).toBeInTheDocument();
    expect(screen.queryByRole("columnheader", { name: "Resolved" })).toBeNull();
  });
});
