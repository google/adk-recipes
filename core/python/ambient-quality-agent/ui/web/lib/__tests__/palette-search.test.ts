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
import { QueryClient } from "@tanstack/react-query";
import {
  buildAgentScope,
  type InsightView,
  type InvestigationListItem,
} from "@/lib/aqua-api";
import {
  isIdQuery,
  listCachedInsights,
  listCachedInvestigations,
  listMatchingInsights,
  listMatchingInvestigations,
  mergeInsightMatches,
} from "@/lib/palette-search";

const buildInsight = (id: string, extra: Partial<InsightView> = {}) =>
  ({
    insight_id: id,
    label: "",
    diagnosis: "",
    ...extra,
  }) as InsightView;

const buildInvestigation = (id: string) =>
  ({ investigation_id: id, status: "done" }) as InvestigationListItem;

describe("isIdQuery", () => {
  it.each([
    ["31fd3807", true],
    ["0372c5aa1133444091d572fe9727bc69", true],
    ["  31fd3807  ", true],
    ["ins-6c1a2b3d4e5f", true],
    ["31fd380", false],
    ["planner loops", false],
    ["planners", false],
  ])("%s -> %s", (query, expected) => {
    expect(isIdQuery(query)).toBe(expected);
  });
});

describe("listMatchingInsights", () => {
  const insights = [
    buildInsight("aaa111", { label: "tool call fails" }),
    buildInsight("bbb222", { refined_label: "planner loops on aaa" }),
    buildInsight("ccc333", { diagnosis: "the TOOL schema is stale" }),
  ];

  it("lists id-prefix matches before text matches", () => {
    expect(
      listMatchingInsights(insights, "aaa", 5).map((i) => i.insight_id),
    ).toEqual(["aaa111", "bbb222"]);
  });

  it("matches label, refined label and diagnosis case-insensitively", () => {
    expect(
      listMatchingInsights(insights, "Tool", 5).map((i) => i.insight_id),
    ).toEqual(["aaa111", "ccc333"]);
  });

  it("returns nothing for an empty query and honors the limit", () => {
    expect(listMatchingInsights(insights, "  ", 5)).toEqual([]);
    expect(listMatchingInsights(insights, "o", 1)).toHaveLength(1);
  });
});

describe("listMatchingInvestigations", () => {
  it("matches the 8-character short id the dashboard shows", () => {
    const full = "31fd3807a9c24e0b8f1d2c3b4a596877";
    expect(
      listMatchingInvestigations(
        [buildInvestigation("ffff0000"), buildInvestigation(full)],
        "31FD3807",
        5,
      ),
    ).toEqual([buildInvestigation(full)]);
  });
});

describe("cached lists", () => {
  it("reads every cached page of the selected agent, each item once", () => {
    const client = new QueryClient();
    client.setQueryData([...buildAgentScope("a"), "insights", 1], {
      insights: [buildInsight("x"), buildInsight("y")],
      total: 2,
    });
    client.setQueryData([...buildAgentScope("a"), "insights", 2], {
      insights: [buildInsight("y"), buildInsight("z")],
      total: 2,
    });
    client.setQueryData([...buildAgentScope("b"), "insights"], {
      insights: [buildInsight("other-agent")],
      total: 1,
    });
    client.setQueryData(
      [...buildAgentScope("a"), "investigations"],
      [buildInvestigation("r1")],
    );
    client.setQueryData(
      [...buildAgentScope("a"), "investigations", "2026-11-01"],
      [buildInvestigation("r1"), buildInvestigation("r2")],
    );

    expect(listCachedInsights(client, "a").map((i) => i.insight_id)).toEqual([
      "x",
      "y",
      "z",
    ]);
    expect(
      listCachedInvestigations(client, "a").map((i) => i.investigation_id),
    ).toEqual(["r1", "r2"]);
  });
});

describe("mergeInsightMatches", () => {
  it("lists loaded matches first, the server's after, each once", () => {
    const merged = mergeInsightMatches(
      [buildInsight("a"), buildInsight("b")],
      [buildInsight("b"), buildInsight("c"), buildInsight("d")],
      3,
    );
    expect(merged.map((i) => i.insight_id)).toEqual(["a", "b", "c"]);
  });
});
