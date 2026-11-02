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

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  buildAgentScope,
  agentsQuery,
  caseConversationQuery,
  dailyQuery,
  deleteMemory,
  dismissInsight,
  effectiveConfigQuery,
  goalQuery,
  goalVersionsQuery,
  healthQuery,
  insightQuery,
  insightsQuery,
  investigationQuery,
  memoriesQuery,
  mergeInsights,
  runsQuery,
  saveGoal,
  scheduleQuery,
  sourceQuery,
  startInvestigation,
  statsQuery,
  buildAgentPath,
} from "../aqua-api";

const KEY = "acme-prod__support";

let fetchMock: ReturnType<typeof vi.fn>;

beforeEach(() => {
  fetchMock = vi.fn(
    async () =>
      new Response(
        JSON.stringify({
          insights: [],
          runs: [],
          days: [],
          stats: {},
          run: {},
          case: {},
        }),
        { status: 200 },
      ),
  );
  vi.stubGlobal("fetch", fetchMock);
});

afterEach(() => {
  vi.unstubAllGlobals();
});

const readRequestedUrl = (): URL =>
  new URL(String(fetchMock.mock.calls[0][0]), "http://x");

describe("buildAgentPath", () => {
  it("adds the key as the first parameter", () => {
    expect(buildAgentPath("/api/health", KEY)).toBe(
      "/api/health?agent_key=acme-prod__support",
    );
  });

  it("adds it after the parameters already there", () => {
    expect(buildAgentPath("/api/insights?status=NEW", KEY)).toBe(
      "/api/insights?status=NEW&agent_key=acme-prod__support",
    );
  });

  it("adds nothing for the default agent", () => {
    expect(buildAgentPath("/api/health", undefined)).toBe("/api/health");
  });
});

// Every agent-scoped read, built for one agent.
const READS = {
  health: (k?: string) => healthQuery(k),
  schedule: (k?: string) => scheduleQuery(k),
  stats: (k?: string) => statsQuery(k),
  daily: (k?: string) => dailyQuery(k),
  runs: (k?: string) => runsQuery(k),
  "runs of a day": (k?: string) => runsQuery(k, "2026-10-01"),
  insights: (k?: string) => insightsQuery(k, { status: "NEW" }),
  insight: (k?: string) => insightQuery(k, "i-1"),
  investigation: (k?: string) => investigationQuery(k, "r-1"),
  case: (k?: string) => caseConversationQuery(k, "r-1", "c-1"),
  source: (k?: string) => sourceQuery(k),
  config: (k?: string) => effectiveConfigQuery(k),
  goal: (k?: string) => goalQuery(k),
  "goal versions": (k?: string) => goalVersionsQuery(k),
  memories: (k?: string) => memoriesQuery(k),
};

describe.each(Object.entries(READS))("the %s read", (_, build) => {
  it("is cached under the agent's scope", () => {
    const key = build(KEY).queryKey;
    expect(key.slice(0, 3)).toEqual([...buildAgentScope(KEY)]);
    expect(build(undefined).queryKey).not.toEqual(key);
  });

  it("asks for the agent's data", async () => {
    await build(KEY).queryFn();
    expect(readRequestedUrl().searchParams.get("agent_key")).toBe(KEY);
  });

  it("asks for the default agent's without a key", async () => {
    await build(undefined).queryFn();
    expect(readRequestedUrl().searchParams.has("agent_key")).toBe(false);
  });
});

describe("the writes", () => {
  it.each([
    ["start", () => startInvestigation(KEY)],
    ["dismiss", () => dismissInsight(KEY, "i-1")],
    [
      "merge",
      () => mergeInsights(KEY, { insightId: "i-1", targetInsightId: "i-2" }),
    ],
    ["save the goal", () => saveGoal(KEY, "g")],
    ["delete a memory", () => deleteMemory(KEY, "m-1")],
  ])("%s for the selected agent", async (_, write) => {
    await write();
    expect(readRequestedUrl().searchParams.get("agent_key")).toBe(KEY);
  });
});

describe("agentsQuery", () => {
  it("is instance-wide, outside every agent's scope", async () => {
    expect(agentsQuery().queryKey).toEqual(["aqua", "agents"]);
    await agentsQuery().queryFn();
    expect(readRequestedUrl().pathname).toBe("/api/agents");
    expect(readRequestedUrl().search).toBe("");
  });
});
