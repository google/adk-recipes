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

import { EffectiveConfigCard } from "../effective-config-card";

function withClient(ui: React.ReactElement) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(<QueryClientProvider client={qc}>{ui}</QueryClientProvider>);
}

function respond(body: unknown) {
  vi.stubGlobal(
    "fetch",
    vi.fn(async () => new Response(JSON.stringify(body), { status: 200 })),
  );
}

describe("EffectiveConfigCard", () => {
  it("shows the model in use, flattened from its nested object", async () => {
    respond({
      config: {
        base_model: { model: "gemini-3.7-flash", location: "global" },
        telemetry_table: "completions_view",
      },
    });
    withClient(<EffectiveConfigCard />);
    expect(
      await screen.findByText("gemini-3.7-flash (global)"),
    ).toBeInTheDocument();
    expect(await screen.findByText("completions_view")).toBeInTheDocument();
  });

  it("renders an unset value as a dash rather than hiding the field", async () => {
    // An empty engine id is the answer someone is looking for, not noise.
    respond({ config: { aqa_engine_id: "" } });
    withClient(<EffectiveConfigCard />);
    expect(await screen.findByText("AQuA's own engine id")).toBeInTheDocument();
    expect(await screen.findByText("AQuA storage")).toBeInTheDocument();
    expect(await screen.findByText("—")).toBeInTheDocument();
  });

  it("says the read failed instead of showing an empty config", async () => {
    respond({ error: "the agent returned no configuration" });
    withClient(<EffectiveConfigCard />);
    expect(
      await screen.findByText(/Couldn't read the configuration/),
    ).toBeInTheDocument();
  });

  it("groups the fields rather than listing them as the payload arrives", async () => {
    respond({
      config: {
        telemetry_table: "completions_view",
        observed_agent_name: "travel_desk_agent",
      },
    });
    withClient(<EffectiveConfigCard />);
    // Two keys, adjacent in the payload, that belong under different headings.
    // By role: "Observed agent" is a group title and also the label of the
    // field inside it, as it was in the old dashboard.
    expect(
      await screen.findByRole("heading", { name: "Observed agent" }),
    ).toBeInTheDocument();
    expect(
      await screen.findByRole("heading", { name: "Telemetry source" }),
    ).toBeInTheDocument();
  });

  it("puts a field it has never heard of in Other rather than dropping it", async () => {
    // The whole point of the bucket: grouping must not be able to hide a key.
    respond({ config: { some_new_setting: "on" } });
    withClient(<EffectiveConfigCard />);
    expect(await screen.findByText("Other")).toBeInTheDocument();
    expect(await screen.findByText("some new setting")).toBeInTheDocument();
  });

  it("renders a list as pills and a boolean as yes/no", async () => {
    respond({
      config: {
        multi_turn_metrics: ["coherence", "helpfulness"],
        sync_investigation: false,
      },
    });
    withClient(<EffectiveConfigCard />);
    expect(await screen.findByText("coherence")).toBeInTheDocument();
    expect(await screen.findByText("helpfulness")).toBeInTheDocument();
    expect(await screen.findByText("no")).toBeInTheDocument();
  });
});
