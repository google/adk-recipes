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
import { isTransportError } from "../is-transport-error";

describe("isTransportError", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("treats TypeError (fetch failure) as transport", () => {
    expect(isTransportError(new TypeError("Failed to fetch"))).toBe(true);
  });

  it("matches network-error messages on plain Error", () => {
    expect(
      isTransportError(new Error("NetworkError when attempting to fetch")),
    ).toBe(true);
    expect(
      isTransportError(new Error("connect ECONNREFUSED 127.0.0.1:8001")),
    ).toBe(true);
  });

  it("returns true when navigator reports offline", () => {
    vi.stubGlobal("navigator", { onLine: false });
    expect(isTransportError(new Error("something else"))).toBe(true);
  });

  it("returns false for application-layer errors", () => {
    expect(isTransportError(new Error("bad JSON in part"))).toBe(false);
    expect(isTransportError(new Error("404 Not Found"))).toBe(false);
  });

  it("handles non-Error throwables", () => {
    expect(isTransportError("Failed to fetch")).toBe(true);
    expect(isTransportError(null)).toBe(false);
    expect(isTransportError(undefined)).toBe(false);
  });
});
