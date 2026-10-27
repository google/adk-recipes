// Copyright 2026 Google LLC
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     https://www.apache.org/licenses/LICENSE-2.0
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { InvestigationsView } from "../investigations-view";

/** Never resolves, so the view stays in its pending state. */
function pendingClient(): QueryClient {
  return new QueryClient({
    defaultOptions: {
      queries: { retry: false, queryFn: () => new Promise(() => {}) },
    },
  });
}

describe("InvestigationsView while loading", () => {
  it("spins instead of rendering an empty page", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(() => new Promise(() => {})),
    );
    render(
      <QueryClientProvider client={pendingClient()}>
        <InvestigationsView />
      </QueryClientProvider>,
    );
    // Name each section: asserting on any role="status" passed even with the
    // funnel and per-day spinners removed, because the runs table has one.
    expect(await screen.findByText("Loading totals…")).toBeInTheDocument();
    expect(
      await screen.findByText("Loading daily counts…"),
    ).toBeInTheDocument();
  });
});
