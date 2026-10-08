// Copyright 2026 Google LLC
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     https://www.apache.org/licenses/LICENSE-2.0

// biome-ignore-all lint/style/noNonNullAssertion: a missing value fails the test either way; the assertion only narrows the type.

import { QueryClient } from "@tanstack/react-query";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { hydrateQueryCache, persistQueryCache } from "../query-persist";

const KEY = "aqua.query-cache.v1";

// happy-dom provides no Storage, so the persister's real code path needs one.
function fakeStorage(): Storage {
  const map = new Map<string, string>();
  return {
    get length() {
      return map.size;
    },
    clear: () => map.clear(),
    getItem: (k: string) => map.get(k) ?? null,
    key: (i: number) => [...map.keys()][i] ?? null,
    removeItem: (k: string) => void map.delete(k),
    setItem: (k: string, v: string) => void map.set(k, v),
  };
}

let store: Storage;

beforeEach(() => {
  store = fakeStorage();
  Object.defineProperty(window, "localStorage", {
    value: store,
    configurable: true,
  });
  vi.useRealTimers();
});

function client(): QueryClient {
  return new QueryClient({ defaultOptions: { queries: { retry: false } } });
}

async function flush(ms = 1200): Promise<void> {
  await new Promise((r) => setTimeout(r, ms));
}

describe("persistQueryCache", () => {
  it("writes aqua reads and skips everything else", async () => {
    const qc = client();
    const stop = persistQueryCache(qc);
    qc.setQueryData(["aqua", "runs"], [{ run_id: "r1" }]);
    qc.setQueryData(["lha", "contexts", "c", "tasks"], [{ id: "t" }]);
    await flush();
    stop();
    const keys = JSON.parse(store.getItem(KEY)!).map(
      (e: { key: string[] }) => e.key[0],
    );
    expect(keys).toEqual(["aqua"]);
  });

  it("flushes on pagehide instead of losing the debounced write", () => {
    // A reload fires pagehide before the debounce elapses. Without the flush a
    // measured run persisted only the three fastest queries.
    const qc = client();
    const stop = persistQueryCache(qc);
    qc.setQueryData(["aqua", "insights"], [{ id: "i1" }]);
    expect(store.getItem(KEY)).toBeNull();
    window.dispatchEvent(new Event("pagehide"));
    stop();
    expect(JSON.parse(store.getItem(KEY)!)).toHaveLength(1);
  });
});

describe("hydrateQueryCache", () => {
  it("restores data so the first paint has content", () => {
    store.setItem(
      KEY,
      JSON.stringify([
        {
          key: ["aqua", "runs"],
          data: [{ run_id: "r1" }],
          updatedAt: Date.now(),
        },
      ]),
    );
    const qc = client();
    hydrateQueryCache(qc);
    expect(qc.getQueryData(["aqua", "runs"])).toEqual([{ run_id: "r1" }]);
  });

  it("keeps the original timestamp so stale data still refetches", () => {
    const old = Date.now() - 60_000;
    store.setItem(
      KEY,
      JSON.stringify([{ key: ["aqua", "runs"], data: [], updatedAt: old }]),
    );
    const qc = client();
    hydrateQueryCache(qc);
    // Restoring with "now" would make a day-old cache look fresh and suppress
    // the background refetch, which is how a dashboard starts lying.
    expect(qc.getQueryState(["aqua", "runs"])?.dataUpdatedAt).toBe(old);
  });

  it("drops entries older than a day", () => {
    store.setItem(
      KEY,
      JSON.stringify([
        {
          key: ["aqua", "runs"],
          data: [{ run_id: "ancient" }],
          updatedAt: Date.now() - 48 * 60 * 60 * 1000,
        },
      ]),
    );
    const qc = client();
    hydrateQueryCache(qc);
    expect(qc.getQueryData(["aqua", "runs"])).toBeUndefined();
  });

  it("survives a corrupt payload instead of blocking boot", () => {
    store.setItem(KEY, "{not json");
    const qc = client();
    expect(() => hydrateQueryCache(qc)).not.toThrow();
    expect(store.getItem(KEY)).toBeNull();
  });
});
