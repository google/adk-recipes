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

import { resolveSubAgent } from "../insight-agent";

describe("resolveSubAgent", () => {
  it("returns the sub-agent a defect is attributed to", () => {
    expect(
      resolveSubAgent({ agent_name: "travel_desk", agent_id: "expense_agent" }),
    ).toBe("expense_agent");
  });

  it("returns nothing for the observed root agent", () => {
    // Every insight of a single-agent deployment looks like this.
    expect(
      resolveSubAgent({ agent_name: "travel_desk", agent_id: "travel_desk" }),
    ).toBeUndefined();
  });

  it("returns nothing for an unattributed insight", () => {
    expect(
      resolveSubAgent({ agent_name: "travel_desk", agent_id: "" }),
    ).toBeUndefined();
    expect(resolveSubAgent({ agent_name: "travel_desk" })).toBeUndefined();
  });
});
