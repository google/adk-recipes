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

import { SourceCard } from "../source-card";

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

const PUBLISHED = {
  available: true,
  revision: "12",
  revision_count: 4,
  created_at: new Date().toISOString(),
  agent_directory: "app",
  file_count: 312,
  total_bytes: 3_257_344,
  truncated_files: 0,
  omitted_files: 0,
  uri: "gs://b/12/manifest.json",
};

describe("SourceCard", () => {
  it("summarizes the manifest and links to it", async () => {
    respond(PUBLISHED);
    withClient(<SourceCard />);

    expect(await screen.findByText("12")).toBeInTheDocument();
    expect(
      screen.getByText(
        /The observed agent's repository, published at deploy time so the chat and the dig phase can cite its code\./,
      ),
    ).toBeInTheDocument();
    expect(screen.getByText(/of 4 kept/)).toBeInTheDocument();
    expect(screen.getByText("312")).toBeInTheDocument();
    expect(screen.getByText("3.1 MB")).toBeInTheDocument();
    expect(screen.getByText("app")).toBeInTheDocument();
    expect(screen.getByRole("link")).toHaveAttribute(
      "href",
      "https://console.cloud.google.com/storage/browser/_details/b/12/manifest.json",
    );
  });

  it("warns when publishing dropped files at the size cap", async () => {
    // "Published" alone would read as healthy while the one large file a
    // diagnosis needed is exactly what was left out.
    respond({ ...PUBLISHED, truncated_files: 2, omitted_files: 5 });
    withClient(<SourceCard />);

    expect(
      await screen.findByText(/2 files truncated and 5 files omitted/),
    ).toBeInTheDocument();
  });

  it("warns when nothing has been published", async () => {
    // Silence here would look identical to a healthy deployment, while the
    // chat answers "I have no access to the code".
    respond({
      available: false,
      reason: "No complete source snapshot has been published to gs://b yet.",
    });
    withClient(<SourceCard />);

    expect(
      await screen.findByText(/No complete source snapshot has been published/),
    ).toBeInTheDocument();
  });

  it("reports a read failure rather than showing it as absent", async () => {
    respond({
      available: false,
      reason: "Forbidden: 403 on the source bucket",
    });
    withClient(<SourceCard />);

    expect(
      await screen.findByText(/403 on the source bucket/),
    ).toBeInTheDocument();
  });
});
