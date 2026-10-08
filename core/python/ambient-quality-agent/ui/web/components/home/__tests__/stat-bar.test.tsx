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

/**
 * The panel's whole job is to be true, so these check what it says, not that
 * it renders.
 */
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import {
  RouterProvider,
  createRootRoute,
  createRoute,
  createRouter,
  createMemoryHistory,
} from "@tanstack/react-router";
import { render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { StatBar } from "../stat-bar";
import type { AmbientHealth } from "@/lib/aqua-api";

const healthy: AmbientHealth = {
  verdict: "watching",
  reason: "the last scheduled investigation ran 6 hours ago",
  schedule: "0 12 * * *",
  max_age_hours: 48,
  last_scheduled_finish: new Date(Date.now() - 6 * 3600_000).toISOString(),
  last_run: {
    run_id: "run-abc",
    finished_at: new Date(Date.now() - 6 * 3600_000).toISOString(),
    status: "done",
    trigger_type: "scheduled",
    sessions: 410,
    sessions_passed: 295,
    pass_rate: 0.7195,
  },
  open_findings: 4,
  worst_finding: {
    insight_id: "i-1",
    label: "The final response carried no text.",
  },
};

function renderPanel(body: unknown, status = 200) {
  vi.stubGlobal(
    "fetch",
    vi.fn(async () => new Response(JSON.stringify(body), { status })),
  );

  const root = createRootRoute();
  const index = createRoute({
    getParentRoute: () => root,
    path: "/",
    component: StatBar,
  });
  const insights = createRoute({
    getParentRoute: () => root,
    path: "/insights",
  });
  const insight = createRoute({
    getParentRoute: () => root,
    path: "/insights/$insightId",
  });
  const run = createRoute({
    getParentRoute: () => root,
    path: "/investigations/$runId",
  });
  const router = createRouter({
    routeTree: root.addChildren([index, insights, insight, run]),
    history: createMemoryHistory({ initialEntries: ["/"] }),
  });
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });

  render(
    <QueryClientProvider client={qc}>
      {/* biome-ignore lint/suspicious/noExplicitAny: the test router is not the app's registered router type. */}
      <RouterProvider router={router as any} />
    </QueryClientProvider>,
  );
}

describe("StatBar", () => {
  beforeEach(() => vi.unstubAllGlobals());
  afterEach(() => vi.unstubAllGlobals());

  it("says AQuA is watching, with the numbers behind it", async () => {
    renderPanel(healthy);

    expect(await screen.findByText("Watching")).toBeInTheDocument();
    expect(screen.getByText(/295 of 410 trajectories/)).toBeInTheDocument();
    expect(screen.getByText("Open insights")).toBeInTheDocument();
    expect(screen.getByText("Last investigation")).toBeInTheDocument();
    expect(
      screen.getByText(/Scheduled investigation run-abc/),
    ).toBeInTheDocument();
    // 0.7195 must read as 72%, not 0.7195 or 71.95.
    expect(screen.getByText("72%")).toBeInTheDocument();
  });

  it("says so when the schedule has never fired", async () => {
    // Simulates a configured schedule that has never finished an investigation run.
    renderPanel({
      ...healthy,
      verdict: "never",
      reason: "no scheduled investigation has ever finished",
      last_scheduled_finish: null,
      last_run: { ...healthy.last_run!, trigger_type: "manual" },
    });

    expect(await screen.findByText("Never run")).toBeInTheDocument();
    expect(screen.queryByText("Watching")).toBeNull();
  });

  it("marks an unhealthy panel visually, not only in words", async () => {
    renderPanel({ ...healthy, verdict: "overdue" });

    const panel = await screen.findByRole("region", {
      name: /ambient health/i,
    });
    expect(panel.className).toMatch(/amber/);
  });

  it("leaves a healthy panel unmarked", async () => {
    renderPanel(healthy);

    const panel = await screen.findByRole("region", {
      name: /ambient health/i,
    });
    expect(panel.className).not.toMatch(/amber/);
  });

  it("reports an unreachable check instead of inventing health", async () => {
    // The defect this codebase keeps shipping: absence rendered as a value.
    // A failed read must never paint "watching" or a row of zeroes.
    renderPanel({ error: "RuntimeError: no direct reads on this backend" });

    expect(await screen.findByText(/Health unavailable/)).toBeInTheDocument();
    expect(screen.queryByText("Watching")).toBeNull();
    expect(screen.queryByText("0")).toBeNull();
  });

  it("writes the schedule as a sentence, not a crontab", async () => {
    renderPanel(healthy);

    // "0 12 * * *" renders as glitched asterisks.
    expect(await screen.findByText(/daily at 12:00 UTC/)).toBeInTheDocument();
    expect(screen.queryByText(/\* \* \*/)).toBeNull();
  });

  it("shows a crontab it cannot phrase rather than guessing", async () => {
    renderPanel({ ...healthy, schedule: "*/15 3 * * 1-5" });

    expect(await screen.findByText("*/15 3 * * 1-5")).toBeInTheDocument();
  });

  it("renders the failing verdict it is given", async () => {
    renderPanel({
      ...healthy,
      verdict: "failing",
      reason: "the most recent investigation ended in an error",
    });

    expect(await screen.findByText("Failing")).toBeInTheDocument();
  });

  it("does not guess when the engine sends a verdict it does not know", async () => {
    // A newer engine's verdict is unknown, not broken. Guessing the worst is
    // the same error as guessing the best.
    renderPanel({ ...healthy, verdict: "degraded", reason: "something new" });

    expect(await screen.findByText("degraded")).toBeInTheDocument();
    expect(screen.queryByText("Failing")).toBeNull();
    expect(screen.queryByText("Watching")).toBeNull();
  });

  it("omits a pass rate the run does not have", async () => {
    // Null, not zero: a run that evaluated nothing has no pass rate, and 0%
    // reads as every trace failing.
    renderPanel({
      ...healthy,
      last_run: { ...healthy.last_run!, sessions: null, pass_rate: null },
    });

    expect(await screen.findByText("Watching")).toBeInTheDocument();
    expect(screen.getByText("--")).toBeInTheDocument();
    expect(screen.getByText(/No trajectories evaluated/)).toBeInTheDocument();
    expect(screen.queryByText(/of .* trajectories/)).toBeNull();
  });

  it("renders Overdue and Disabled verdicts with canonical labels", async () => {
    renderPanel({
      ...healthy,
      verdict: "overdue",
      reason: "the last scheduled investigation is past due",
    });
    expect(await screen.findByText("Overdue")).toBeInTheDocument();

    renderPanel({
      ...healthy,
      verdict: "disabled",
      reason: "ambient scheduling is turned off",
    });
    expect(await screen.findByText("Disabled")).toBeInTheDocument();
  });
});
