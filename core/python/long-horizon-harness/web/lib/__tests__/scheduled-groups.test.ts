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
import { groupByJobType } from "../scheduled-groups";
import type { HorizonSessionSummary } from "../horizon-sessions";

function s(id: string, jobType: HorizonSessionSummary["jobType"]): HorizonSessionSummary {
  return {
    id,
    title: id,
    createdAt: 0,
    lastUpdated: 0,
    source: "scheduler",
    jobType,
    workspaceWindow: [],
  };
}

describe("groupByJobType", () => {
  it("orders Dream review before Routines and drops empty groups", () => {
    const groups = groupByJobType([
      s("a", "routine"),
      s("b", "dream_review"),
    ]);
    expect(groups.map((g) => g.label)).toEqual(["Dream review", "Routines"]);
    expect(groups[0].sessions.map((x) => x.id)).toEqual(["b"]);
  });

  it("buckets unknown/null jobType under Other", () => {
    const groups = groupByJobType([s("a", null)]);
    expect(groups.map((g) => g.label)).toEqual(["Other"]);
  });
});
