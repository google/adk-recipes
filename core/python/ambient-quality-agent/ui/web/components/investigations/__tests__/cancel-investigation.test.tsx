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

import { beforeEach, describe, expect, it, vi } from "vitest";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import {
  createMemoryHistory,
  createRootRoute,
  createRoute,
  createRouter,
  RouterProvider,
} from "@tanstack/react-router";
import { InvestigationDetailView } from "../investigation-detail-view";
import type { Investigation } from "@/lib/aqua-api";
import { counters } from "@/lib/__fixtures__/counters";

const INVESTIGATION_ID = "inv-12345678";

const PENDING: Investigation = {
  investigation_id: INVESTIGATION_ID,
  status: "pending",
  observed_agent_key: "it_support_agent",
  trigger_type: "manual",
  created_at: new Date().toISOString(),
  finished_at: null,
  elapsed_seconds: null,
  metrics_passed: 0,
  metrics_failed: 0,
  metrics_errored: 0,
  counters: counters({}),
  summary: {},
  events: [],
  error: null,
};

/** Serves the investigation and the cancel route, as the dashboard server does.
 *
 * Returns the cancel calls, so a test can tell whether anything was sent.
 */
function serve(
  record: Investigation,
  cancel: { status: number; body: unknown } = {
    status: 200,
    body: { investigation: { ...record, status: "cancelled" } },
  },
) {
  let current = record;
  const cancelCalls: string[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn().mockImplementation((url: string, init?: RequestInit) => {
      if (url.includes("/api/insights")) {
        return Promise.resolve(
          new Response(JSON.stringify({ insights: [], total: 0 }), {
            status: 200,
          }),
        );
      }
      if (url.includes("/cancel") && init?.method === "POST") {
        cancelCalls.push(url);
        // A refusal means it finished meanwhile; a refetch would see it.
        current = {
          ...current,
          status: cancel.status === 200 ? "cancelled" : "done",
        };
        return Promise.resolve(
          new Response(JSON.stringify(cancel.body), { status: cancel.status }),
        );
      }
      if (url.includes("/api/investigations/")) {
        return Promise.resolve(
          new Response(JSON.stringify({ investigation: current }), {
            status: 200,
          }),
        );
      }
      return Promise.resolve(
        new Response(JSON.stringify({ runs: [] }), { status: 200 }),
      );
    }),
  );
  return cancelCalls;
}

function renderDetail() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: 0 } },
  });
  const rootRoute = createRootRoute();
  const indexRoute = createRoute({
    getParentRoute: () => rootRoute,
    path: "/",
    component: () => (
      <InvestigationDetailView investigationId={INVESTIGATION_ID} />
    ),
  });
  const router = createRouter({
    routeTree: rootRoute.addChildren([indexRoute]),
    history: createMemoryHistory({ initialEntries: ["/"] }),
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <RouterProvider router={router} />
    </QueryClientProvider>,
  );
}

async function openDialog() {
  await userEvent.click(
    await screen.findByRole("button", { name: "Cancel investigation" }),
  );
  return screen.findByRole("alertdialog");
}

describe("Cancel investigation", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });

  it.each(["scheduled", "pending", "running"])(
    "is offered for a %s investigation",
    async (status) => {
      serve({ ...PENDING, status });
      renderDetail();

      expect(
        await screen.findByRole("button", { name: "Cancel investigation" }),
      ).toBeInTheDocument();
    },
  );

  it.each(["done", "failed", "skipped", "cancelled"])(
    "is not offered for a %s investigation",
    async (status) => {
      serve({ ...PENDING, status });
      renderDetail();

      await screen.findByRole("heading", { level: 1 });
      expect(
        screen.queryByRole("button", { name: "Cancel investigation" }),
      ).not.toBeInTheDocument();
    },
  );

  it("asks first, and sends nothing when the user keeps it running", async () => {
    const calls = serve(PENDING);
    renderDetail();

    const dialog = await openDialog();
    expect(dialog).toHaveTextContent("Cancel this investigation?");
    expect(dialog).toHaveTextContent("creates no insights");
    await userEvent.click(screen.getByRole("button", { name: "Keep running" }));

    await waitFor(() =>
      expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument(),
    );
    expect(calls).toEqual([]);
  });

  it("cancels once confirmed and shows it as cancelled", async () => {
    const calls = serve(PENDING);
    renderDetail();

    const dialog = await openDialog();
    await userEvent.click(
      within(dialog).getByRole("button", { name: "Cancel investigation" }),
    );

    await waitFor(() =>
      expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument(),
    );
    expect(calls).toEqual([`/api/investigations/${INVESTIGATION_ID}/cancel`]);
    const badge = await screen.findByText("Cancelled");
    // A cancel is not a success: the badge is muted, not green.
    expect(badge).not.toHaveClass("text-emerald-500");
    expect(badge).toHaveClass("text-muted-foreground");
    expect(
      screen.queryByRole("button", { name: "Cancel investigation" }),
    ).not.toBeInTheDocument();
  });

  it("shows the agent's reason when it refuses", async () => {
    serve(PENDING, {
      status: 409,
      body: {
        error: "investigation 'inv-12345678' has already finished as done",
      },
    });
    renderDetail();

    const dialog = await openDialog();
    await userEvent.click(
      within(dialog).getByRole("button", { name: "Cancel investigation" }),
    );

    expect(await within(dialog).findByRole("alert")).toHaveTextContent(
      "already finished as done",
    );
    // It is not refetched while the reason is on screen, so the dialog
    // stays rather than unmounting with the button.
    await new Promise((resolve) => setTimeout(resolve, 50));
    expect(within(dialog).getByRole("alert")).toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: "Keep running" }));
    // Closing it refreshes the investigation, which has finished, so the
    // button goes.
    await waitFor(() =>
      expect(
        screen.queryByRole("button", { name: "Cancel investigation" }),
      ).not.toBeInTheDocument(),
    );
  });

  it("gives focus back to the button when the dialog closes", async () => {
    serve(PENDING);
    renderDetail();

    await openDialog();
    await userEvent.click(screen.getByRole("button", { name: "Keep running" }));

    await waitFor(() =>
      expect(
        screen.getByRole("button", { name: "Cancel investigation" }),
      ).toHaveFocus(),
    );
  });
});
