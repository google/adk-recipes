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
import { useRoutines } from "../horizon-routines";

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
          JSON.stringify({
            routines: [
              {
                id: "daily-thing",
                task: "do the daily thing",
                schedule: "0 9 * * *",
                next_fire_at: "2026-06-17T09:00:00+00:00",
                secrets: [],
                delivery: "report_back",
              },
            ],
          }),
          { status: 200 },
        ),
    ),
  );
});

describe("useRoutines", () => {
  it("loads routines", async () => {
    const { result } = renderHook(() => useRoutines(), { wrapper: wrapper() });
    await waitFor(() =>
      expect(result.current.data?.routines).toHaveLength(1),
    );
    expect(result.current.data?.routines[0]?.id).toBe("daily-thing");
    expect(result.current.data?.routines[0]?.schedule).toBe("0 9 * * *");
  });

  it("populates the TanStack Query cache under qk.routines()", async () => {
    const client = makeQueryClient();
    const { result } = renderHook(() => useRoutines(), {
      wrapper: wrapper(client),
    });
    await waitFor(() => expect(result.current.isLoading).toBe(false));
    const cached = client.getQueryData<{ routines: { id: string }[] }>(
      qk.routines(),
    );
    expect(cached?.routines).toHaveLength(1);
  });
});
