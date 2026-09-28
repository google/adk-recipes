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

import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { renderHook } from "@testing-library/react";
import { QueryClientProvider } from "@tanstack/react-query";
import { createElement, type ReactNode } from "react";
import { makeQueryClient } from "../query-client";
import { useSandboxStatus } from "../horizon-sandbox-status";

const W = (c = makeQueryClient()) =>
  ({ children }: { children: ReactNode }) =>
    createElement(QueryClientProvider, { client: c }, children);

beforeEach(() => vi.useFakeTimers());
afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

describe("useSandboxStatus", () => {
  it("polls ~500ms while provisioning, stops once ready", async () => {
    const fetchSpy = vi
      .fn()
      .mockResolvedValueOnce(
        new Response(JSON.stringify({ status: "provisioning" })),
      )
      .mockResolvedValue(new Response(JSON.stringify({ status: "ready" })));
    vi.stubGlobal("fetch", fetchSpy);

    renderHook(() => useSandboxStatus(), { wrapper: W() });

    // initial fetch + at least one 500ms poll while provisioning
    await vi.advanceTimersByTimeAsync(600);
    expect(fetchSpy.mock.calls.length).toBeGreaterThanOrEqual(2);

    const afterReady = fetchSpy.mock.calls.length;

    // status is now "ready" → polling should have stopped
    await vi.advanceTimersByTimeAsync(3000);
    expect(fetchSpy.mock.calls.length).toBe(afterReady);
  });
});
