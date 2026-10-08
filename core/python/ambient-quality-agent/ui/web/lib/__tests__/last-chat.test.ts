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

import { forgetLastChat, readLastChat, rememberLastChat } from "../last-chat";

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
    expect(() => forgetLastChat("ctx-a")).not.toThrow();
    expect(() => forgetLastChat()).not.toThrow();
  });

  it("reads, remembers and forgets nothing when localStorage itself is denied", () => {
    vi.spyOn(window, "localStorage", "get").mockImplementation(deny);

    expect(readLastChat()).toBeNull();
    expect(() => rememberLastChat("ctx-a")).not.toThrow();
    expect(() => forgetLastChat()).not.toThrow();
  });
});
