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

import { describe, expect, it, vi, beforeEach } from "vitest";

const { toastFn } = vi.hoisted(() => ({
  toastFn: Object.assign(vi.fn(), {
    success: vi.fn(),
    error: vi.fn(),
    info: vi.fn(),
  }),
}));
vi.mock("sonner", () => ({ toast: toastFn }));

import { notify } from "@/lib/toast";

describe("notify", () => {
  beforeEach(() => {
    toastFn.mockClear();
    toastFn.success.mockClear();
    toastFn.error.mockClear();
    toastFn.info.mockClear();
  });

  it("forwards error() to sonner toast.error with description", () => {
    notify.error("Reconnect failed", { description: "network down" });
    expect(toastFn.error).toHaveBeenCalledWith("Reconnect failed", {
      description: "network down",
    });
  });

  it("forwards success() to sonner toast.success", () => {
    notify.success("Copied");
    expect(toastFn.success).toHaveBeenCalledWith("Copied", undefined);
  });

  it("forwards message() to the base toast()", () => {
    notify.message("Heads up");
    expect(toastFn).toHaveBeenCalledWith("Heads up", undefined);
  });
});
