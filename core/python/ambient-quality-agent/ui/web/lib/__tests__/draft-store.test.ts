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

import { describe, expect, it, vi, beforeEach, afterEach } from "vitest";
import { loadDraft, saveDraft, clearDraft } from "@/lib/draft-store";

// Node 26 ships a built-in `localStorage` that is unavailable without
// --localstorage-file and shadows happy-dom's; stub a Map-backed Storage so the
// store's logic is what's under test (the browser has a real localStorage).
describe("draft-store", () => {
  beforeEach(() => {
    const store = new Map<string, string>();
    vi.stubGlobal("localStorage", {
      getItem: (k: string) => store.get(k) ?? null,
      setItem: (k: string, v: string) => {
        store.set(k, v);
      },
      removeItem: (k: string) => {
        store.delete(k);
      },
      clear: () => store.clear(),
      key: (i: number) => [...store.keys()][i] ?? null,
      get length() {
        return store.size;
      },
    });
  });
  afterEach(() => vi.unstubAllGlobals());

  it("round-trips a draft by contextId", () => {
    saveDraft("ctx-1", "hello world");
    expect(loadDraft("ctx-1")).toBe("hello world");
    expect(loadDraft("ctx-2")).toBe("");
  });

  it("clears when saving blank text", () => {
    saveDraft("ctx-1", "text");
    saveDraft("ctx-1", "   ");
    expect(loadDraft("ctx-1")).toBe("");
  });

  it("clearDraft removes the entry", () => {
    saveDraft("ctx-1", "text");
    clearDraft("ctx-1");
    expect(loadDraft("ctx-1")).toBe("");
  });

  it("no-ops for undefined contextId", () => {
    expect(() => saveDraft(undefined, "x")).not.toThrow();
    expect(loadDraft(undefined)).toBe("");
  });

  it("evicts the oldest entry past the cap of 50", () => {
    for (let i = 0; i < 55; i++) saveDraft(`ctx-${i}`, `d${i}`);
    expect(loadDraft("ctx-0")).toBe("");
    expect(loadDraft("ctx-54")).toBe("d54");
  });
});
