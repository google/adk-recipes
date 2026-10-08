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
 * A read that hangs has to become a failure someone can act on.
 *
 * Every dashboard read goes through the agent, so slow is normal and stuck
 * looks identical — without a bound, a wedged backend renders as a spinner
 * that never resolves. These pin the bound; `lib/failure` is what tells the
 * resulting failures apart for a reader.
 */
import { afterEach, describe, expect, it, vi } from "vitest";

import { READ_TIMEOUT_MS, ReadTimeoutError, runsQuery } from "../aqua-api";

afterEach(() => {
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

describe("the read timeout", () => {
  it("aborts a read that outlives it", async () => {
    vi.useFakeTimers();
    vi.stubGlobal(
      "fetch",
      vi.fn(
        (_url: string, init?: RequestInit) =>
          new Promise((_resolve, reject) => {
            init?.signal?.addEventListener("abort", () =>
              reject(new DOMException("Aborted", "AbortError")),
            );
          }),
      ),
    );

    const pending = runsQuery().queryFn();
    const assertion = expect(pending).rejects.toBeInstanceOf(ReadTimeoutError);
    await vi.advanceTimersByTimeAsync(READ_TIMEOUT_MS + 1);
    await assertion;
  });

  it("leaves a read that answers in time alone", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(
        async () => new Response(JSON.stringify({ runs: [] }), { status: 200 }),
      ),
    );
    await expect(runsQuery().queryFn()).resolves.toEqual([]);
  });
});
