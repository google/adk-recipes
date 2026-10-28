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

import { describe, it, expect, beforeEach } from "vitest";
import type { Task } from "@a2a-js/sdk";
import {
  getTaskHistoryCache,
  _resetTaskHistoryCache,
  type TaskCacheEntry,
} from "@/lib/task-history-cache";

const entry = (): TaskCacheEntry => ({
  status: "completed",
  task: {} as Task,
});

beforeEach(() => _resetTaskHistoryCache());

describe("task-history-cache", () => {
  it("returns the same Map instance for a contextId", () => {
    const a = getTaskHistoryCache("x");
    a.set("t1", entry());
    expect(getTaskHistoryCache("x")).toBe(a);
    expect(getTaskHistoryCache("x").get("t1")).toBeDefined();
  });

  it("keeps at most 5 chats, evicting the least-recently-used", () => {
    for (const id of ["a", "b", "c", "d", "e"]) {
      getTaskHistoryCache(id).set("t", entry());
    }
    // Touch "a" so "b" becomes the least-recently-used.
    getTaskHistoryCache("a");
    // 6th distinct chat evicts "b".
    getTaskHistoryCache("f");
    expect(getTaskHistoryCache("b").size).toBe(0); // recreated empty after eviction
    expect(getTaskHistoryCache("a").get("t")).toBeDefined(); // survived
  });
});
