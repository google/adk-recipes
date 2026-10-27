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

import { describe, expect, it, vi, beforeEach } from "vitest";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryCard } from "../memory-card";

function renderCard(fetchImpl: typeof fetch) {
  vi.stubGlobal("fetch", fetchImpl);
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: 0 } },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryCard />
    </QueryClientProvider>,
  );
}

const MEMORY = {
  id: "aaaaaaaaaaaa",
  text: "The **system prompt** is in `app/prompts/system.md`.",
  source: "0a3be866-37c3-496b-a7fb-c3433d3d737d",
  created_at: "2026-09-30T09:00:00+00:00",
};

/** Serves the memories, and records every other request. */
function serve(
  memories: object[],
  requests: { url: string; method?: string }[] = [],
): typeof fetch {
  return (async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = typeof input === "string" ? input : input.toString();
    if (init?.method && init.method !== "GET") {
      requests.push({ url, method: init.method });
      return new Response(JSON.stringify({ deleted: true }), { status: 200 });
    }
    return new Response(JSON.stringify({ memories, available: true }), {
      status: 200,
    });
  }) as typeof fetch;
}

describe("MemoryCard", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });

  it("is titled Memory", async () => {
    renderCard(serve([]));

    expect(
      await screen.findByRole("heading", { name: "Memory" }),
    ).toBeInTheDocument();
  });

  it("shows a memory as plain text, with where it came from and Delete", async () => {
    renderCard(serve([MEMORY]));

    // Plain text: the chat worded it, so no Markdown is rendered from it.
    const item = (await screen.findByText(MEMORY.text)).closest("li")!;
    expect(within(item).getByText(/from chat 0a3be866/)).toBeInTheDocument();
    expect(
      within(item).getByRole("button", { name: /Delete memory/ }),
    ).toBeInTheDocument();
  });

  it("deletes a memory", async () => {
    const requests: { url: string; method?: string }[] = [];
    renderCard(serve([MEMORY], requests));

    await userEvent.click(
      await screen.findByRole("button", { name: /Delete memory/ }),
    );

    expect(requests.map((r) => [r.method, r.url])).toEqual([
      ["DELETE", `/api/memories/${MEMORY.id}`],
    ]);
  });

  it("says how a memory gets here when there is none", async () => {
    renderCard(serve([]));

    expect(
      await screen.findByText(
        /Ask the chat to remember something about your agent/,
      ),
    ).toBeInTheDocument();
  });

  it("names why the memories cannot be read", async () => {
    renderCard((() =>
      Promise.resolve(
        new Response(
          JSON.stringify({
            memories: [],
            available: false,
            reason: "No jobs bucket.",
          }),
          { status: 200 },
        ),
      )) as typeof fetch);

    expect(await screen.findByText(/No jobs bucket\./)).toBeInTheDocument();
  });
});
