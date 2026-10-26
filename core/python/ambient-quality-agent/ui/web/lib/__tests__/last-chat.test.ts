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

import { afterEach, describe, expect, it, vi } from "vitest";

import {
  forgetDeletedChat,
  forgetLastChat,
  readLastChat,
  rememberLastChat,
} from "../last-chat";

function deny(): never {
  throw new DOMException("Access is denied", "SecurityError");
}

function exceedQuota(): never {
  throw new DOMException("The quota has been exceeded", "QuotaExceededError");
}

function stubStorage(storage: Partial<Storage>) {
  vi.spyOn(window, "localStorage", "get").mockReturnValue(storage as Storage);
}

afterEach(() => {
  vi.restoreAllMocks();
});

describe("last chat when localStorage throws", () => {
  it("remembers nothing once storage is full, without throwing", () => {
    stubStorage({ getItem: () => null, setItem: exceedQuota });

    expect(() => rememberLastChat("ctx-a")).not.toThrow();
  });

  it("reads and forgets nothing when every storage call throws", () => {
    stubStorage({ getItem: deny, setItem: deny, removeItem: deny });

    expect(readLastChat()).toBeNull();
    expect(() => forgetDeletedChat("ctx-a")).not.toThrow();
    expect(() => forgetLastChat()).not.toThrow();
  });

  it("reads, remembers and forgets nothing when localStorage itself is denied", () => {
    vi.spyOn(window, "localStorage", "get").mockImplementation(deny);

    expect(readLastChat()).toBeNull();
    expect(() => rememberLastChat("ctx-a")).not.toThrow();
    expect(() => forgetLastChat()).not.toThrow();
  });
});

function createFakeStorage(): Storage {
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

describe("last chat per agent", () => {
  it("keeps one for each agent, so a switch never reopens another's", () => {
    stubStorage(createFakeStorage());

    rememberLastChat("ctx-default");
    rememberLastChat("ctx-prod", "acme-prod__support");

    expect(readLastChat()).toBe("ctx-default");
    expect(readLastChat("acme-prod__support")).toBe("ctx-prod");
    expect(readLastChat("acme-staging__support")).toBeNull();
  });

  it("forgets only the agent's own on a new chat", () => {
    stubStorage(createFakeStorage());
    rememberLastChat("ctx-default");
    rememberLastChat("ctx-prod", "acme-prod__support");

    forgetLastChat("acme-prod__support");

    expect(readLastChat("acme-prod__support")).toBeNull();
    expect(readLastChat()).toBe("ctx-default");
  });

  it("forgets a deleted chat for whichever agent it was the last of", () => {
    stubStorage(createFakeStorage());
    rememberLastChat("ctx-a");
    rememberLastChat("ctx-a", "acme-prod__support");
    rememberLastChat("ctx-b", "acme-staging__support");

    forgetDeletedChat("ctx-a");

    expect(readLastChat()).toBeNull();
    expect(readLastChat("acme-prod__support")).toBeNull();
    expect(readLastChat("acme-staging__support")).toBe("ctx-b");
  });
});
