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

import { describe, it, expect, vi, beforeEach } from "vitest";
import { renderHook, waitFor } from "@testing-library/react";
import { createElement, type ReactNode } from "react";
import { QueryClientProvider } from "@tanstack/react-query";
import { makeQueryClient } from "../query-client";
import { qk } from "../query-keys";
import { useLhaSecrets } from "../horizon-secrets";

function wrapper(client = makeQueryClient()) {
  return ({ children }: { children: ReactNode }) =>
    createElement(QueryClientProvider, { client }, children);
}

beforeEach(() => {
  vi.stubGlobal(
    "fetch",
    vi.fn(
      async () =>
        new Response(
          JSON.stringify({ secrets: [{ name: "A", created_at: null }] }),
          { status: 200 },
        ),
    ),
  );
});

describe("useLhaSecrets", () => {
  it("loads secrets", async () => {
    const { result } = renderHook(() => useLhaSecrets(), { wrapper: wrapper() });
    await waitFor(() =>
      expect(result.current.data?.secrets).toHaveLength(1),
    );
    expect(result.current.data?.secrets[0]?.name).toBe("A");
  });

  it("populates the TanStack Query cache under qk.secrets()", async () => {
    const client = makeQueryClient();
    const { result } = renderHook(() => useLhaSecrets(), {
      wrapper: wrapper(client),
    });
    await waitFor(() => expect(result.current.isLoading).toBe(false));
    const cached = client.getQueryData<{ secrets: { name: string }[] }>(
      qk.secrets(),
    );
    expect(cached?.secrets).toHaveLength(1);
  });

  it("refresh() invalidates the query and refetches", async () => {
    const { result } = renderHook(() => useLhaSecrets(), { wrapper: wrapper() });
    await waitFor(() => expect(result.current.isLoading).toBe(false));
    const calls = (fetch as unknown as { mock: { calls: unknown[] } }).mock.calls
      .length;
    await result.current.refresh();
    await waitFor(() =>
      expect(
        (fetch as unknown as { mock: { calls: unknown[] } }).mock.calls.length,
      ).toBeGreaterThan(calls),
    );
  });
});
