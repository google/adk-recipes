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

import { beforeEach, describe, expect, it } from "vitest";
import {
  listConversations,
  rememberConversation,
  resetConversationsForTest,
} from "@/lib/aqua-conversations";
import { isChatContextLive, resolveInsightChats } from "@/lib/insight-chats";

const DEFAULT_SELECTION = { shownKey: "support", defaultKey: "support" };

beforeEach(() => {
  resetConversationsForTest();
});

describe("rememberConversation insight association", () => {
  it("stores explicit insightId and preserves it on subsequent turns", () => {
    rememberConversation("ctx-1", "initial question", "support", "ins-1");
    rememberConversation("ctx-1", "follow-up question", "support");

    const stored = listConversations().find((c) => c.id === "ctx-1");
    expect(stored?.insightId).toBe("ins-1");
  });

  it("leaves insightId undefined when not started from an insight action", () => {
    rememberConversation("ctx-2", "general question", "support");
    const stored = listConversations().find((c) => c.id === "ctx-2");
    expect(stored?.insightId).toBeUndefined();
  });
});

describe("resolveInsightChats", () => {
  it("merges local conversations, server contexts, and root-cause context IDs ordered newest first", () => {
    const threads = resolveInsightChats({
      insightId: "ins-1",
      rootCauseContextIds: ["ctx-rc", "ctx-server"],
      rootCauseTimestamps: new Map([["ctx-rc", 1_500]]),
      conversations: [
        {
          id: "ctx-local",
          title: "Local investigation",
          createdAt: 1_000,
          lastUpdated: 2_000,
          agentKey: "support",
          insightId: "ins-1",
        },
        {
          id: "ctx-other-insight",
          title: "Other insight",
          createdAt: 1_000,
          lastUpdated: 9_000,
          agentKey: "support",
          insightId: "ins-2",
        },
      ],
      serverContexts: [
        {
          context_id: "ctx-server",
          task_ids: ["t-1"],
          updated_at: new Date(3_000).toISOString(),
          title: "Renamed RCA thread",
          first_message: "why did this happen?",
          agent_key: "support",
          insight_id: "ins-1",
        },
        {
          context_id: "ctx-rc",
          task_ids: ["t-2"],
          updated_at: new Date(1_500).toISOString(),
          title: null,
          first_message: "ad-hoc diagnosis chat",
          agent_key: "support",
          insight_id: null,
        },
      ],
      authoritativeServer: true,
      selection: DEFAULT_SELECTION,
    });

    expect(threads).toEqual([
      {
        id: "ctx-server",
        title: "Renamed RCA thread",
        lastUpdated: 3_000,
        hasRootCause: true,
      },
      {
        id: "ctx-local",
        title: "Local investigation",
        lastUpdated: 2_000,
        hasRootCause: false,
      },
      {
        id: "ctx-rc",
        title: "ad-hoc diagnosis chat",
        lastUpdated: 1_500,
        hasRootCause: true,
      },
    ]);
  });

  it("ignores conversations without a matching structured insightId", () => {
    const threads = resolveInsightChats({
      insightId: "ins-1",
      conversations: [
        {
          id: "ctx-unassociated-local",
          title: 'Diagnose insight ins-1 ("timeout on search")',
          derivedTitle: true,
          createdAt: 1_000,
          lastUpdated: 1_000,
        },
      ],
      serverContexts: [
        {
          context_id: "ctx-unassociated-remote",
          task_ids: ["t-1"],
          updated_at: new Date(2_000).toISOString(),
          title: null,
          first_message: 'Diagnose insight ins-1 ("timeout on search")',
          agent_key: null,
          insight_id: null,
        },
      ],
      authoritativeServer: true,
      selection: DEFAULT_SELECTION,
    });

    expect(threads).toEqual([]);
  });

  it("drops deleted root-cause context IDs when /lha/contexts is authoritative, and keeps them on 501 fallback", () => {
    expect(isChatContextLive("ctx-deleted", [], [], true)).toBe(false);
    expect(isChatContextLive("ctx-deleted", [], [], false)).toBe(true);

    const authoritative = resolveInsightChats({
      insightId: "ins-1",
      rootCauseContextIds: ["ctx-deleted"],
      conversations: [],
      serverContexts: [],
      authoritativeServer: true,
      selection: DEFAULT_SELECTION,
    });
    expect(authoritative).toEqual([]);

    const fallback = resolveInsightChats({
      insightId: "ins-1",
      rootCauseContextIds: ["ctx-live-in-memory"],
      conversations: [],
      serverContexts: [],
      authoritativeServer: false,
      selection: DEFAULT_SELECTION,
    });
    expect(fallback).toEqual([
      {
        id: "ctx-live-in-memory",
        title: "Root cause chat",
        lastUpdated: 0,
        hasRootCause: true,
      },
    ]);
  });

  it("excludes conversations belonging to another agent", () => {
    const threads = resolveInsightChats({
      insightId: "ins-1",
      rootCauseContextIds: ["ctx-other-agent"],
      conversations: [
        {
          id: "ctx-other-agent",
          title: "Other agent chat",
          createdAt: 1_000,
          lastUpdated: 1_000,
          agentKey: "billing_agent",
          insightId: "ins-1",
        },
      ],
      serverContexts: [],
      authoritativeServer: false,
      selection: DEFAULT_SELECTION,
    });

    expect(threads).toEqual([]);
  });
});
