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
import { screen, waitFor } from "@testing-library/react";
import {
  listConversations,
  rememberConversation,
  resetConversationsForTest,
} from "../aqua-conversations";
import { deleteAllLhaSessions, useLhaSessions } from "../horizon-sessions";
import { renderWithAgent, stubAgentsFetch } from "./render-with-agent";

const AGENTS = [{ agent_key: "support" }, { agent_key: "acme-prod__support" }];

const CONTEXTS = [
  {
    context_id: "remote-prod",
    task_ids: ["t1"],
    updated_at: "2026-10-01T10:00:00Z",
    agent_key: "acme-prod__support",
  },
  {
    context_id: "remote-unnamed",
    task_ids: ["t2"],
    updated_at: "2026-10-01T09:00:00Z",
    agent_key: null,
  },
];

function SessionIds() {
  const { data } = useLhaSessions();
  return <output>{(data ?? []).map((s) => s.id).join(",")}</output>;
}

beforeEach(() => {
  resetConversationsForTest();
  rememberConversation("local-prod", "q", "acme-prod__support");
  rememberConversation("local-default", "q", "support");
  vi.stubGlobal(
    "fetch",
    stubAgentsFetch(AGENTS, "support", (url) =>
      url.startsWith("/lha/contexts") ? { contexts: CONTEXTS } : {},
    ),
  );
});

afterEach(() => {
  vi.unstubAllGlobals();
});

const readListedIds = () =>
  (screen.getByRole("status").textContent ?? "").split(",").sort();

describe("the chat list", () => {
  it("lists only the selected agent's conversations", async () => {
    renderWithAgent(<SessionIds />, { url: "/?agent=acme-prod__support" });

    await waitFor(() =>
      expect(readListedIds()).toEqual(["local-prod", "remote-prod"]),
    );
  });

  it("lists the conversations that name no agent with the default agent", async () => {
    renderWithAgent(<SessionIds />);

    await waitFor(() =>
      expect(readListedIds()).toEqual(["local-default", "remote-unnamed"]),
    );
  });
});

describe("rememberConversation", () => {
  it("keeps the agent of the first send", () => {
    rememberConversation("local-prod", "later", "support");

    expect(
      listConversations().find((c) => c.id === "local-prod")?.agentKey,
    ).toBe("acme-prod__support");
  });
});

describe("deleteAllLhaSessions", () => {
  it("deletes only the shown agent's conversations", async () => {
    const deleted: string[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url: string, init?: RequestInit) => {
        if (init?.method === "DELETE") {
          deleted.push(url);
          return new Response("{}", { status: 200 });
        }
        return new Response(JSON.stringify({ contexts: CONTEXTS }), {
          status: 200,
        });
      }),
    );

    await expect(
      deleteAllLhaSessions({
        shownKey: "acme-prod__support",
        defaultKey: "support",
      }),
    ).resolves.toEqual({ deleted: 2, failed: 0 });

    expect(deleted.sort()).toEqual([
      "/lha/contexts/local-prod",
      "/lha/contexts/remote-prod",
    ]);
    expect(listConversations().map((c) => c.id)).toEqual(["local-default"]);
  });
});
