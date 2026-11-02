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
import { formatSentenceCase } from "@/lib/utils";

describe("formatSentenceCase", () => {
  it("capitalizes the first letter and leaves the rest alone", () => {
    expect(formatSentenceCase("errors")).toBe("Errors");
    expect(formatSentenceCase("no data")).toBe("No data");
    expect(formatSentenceCase("AQuA run")).toBe("AQuA run");
    expect(formatSentenceCase("")).toBe("");
  });
});
