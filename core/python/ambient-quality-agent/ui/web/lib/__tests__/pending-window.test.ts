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

import { describe, expect, it } from "vitest";
import { setPendingWindow, takePendingWindow } from "@/lib/pending-window";

describe("pending-window", () => {
  it("take returns the dirs and consumes them (once)", () => {
    setPendingWindow("c1", ["projA"]);
    expect(takePendingWindow("c1")).toEqual(["projA"]);
    expect(takePendingWindow("c1")).toBeUndefined();
  });

  it("returns undefined for an unknown contextId", () => {
    expect(takePendingWindow("nope")).toBeUndefined();
  });
});
