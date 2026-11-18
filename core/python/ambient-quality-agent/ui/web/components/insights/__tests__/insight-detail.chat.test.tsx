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

/**
 * Tests for the insight detail header chat action and root-cause chat link,
 * verifying 1-click new chat when undiagnosed, 1-click latest-session open +
 * caret Popover when existing RCA threads exist, and the Popover guard for
 * unlinked or deleted root-cause sessions.
 */
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import {
  createMemoryHistory,
  createRootRoute,
  createRoute,
  createRouter,
  RouterProvider,
} from "@tanstack/react-router";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { InsightView, RootCause } from "@/lib/aqua-api";
import {
  rememberConversation,
  resetConversationsForTest,
} from "@/lib/aqua-conversations";
import type { StoredContext } from "@/lib/lha-contexts";
import { InsightDetailView } from "../insight-detail";
import { insightRcaPrompt } from "../insight-rca-prompt";

const INSIGHT: InsightView = {
  insight_id: "9f2c4b7e1a8d4c3fae5b60d7c1290f34",
  observed_agent_key: "it_support_agent",
  label: "crashed with priority",
  tool_name: "create_ticket",
  status: "RECURRING",
  occurrence_count: 7,
  trajectory_count: 134,
  impact: 400,
  last_investigation_at: "2026-08-28T18:35:34Z",
  diagnosis: "Priority value must be High, Medium, Low.",
  confidence: 1.0,
};

const ROOT_CAUSE: RootCause = {
  root_cause_id: "rc-1",
  insight_id: INSIGHT.insight_id,
  occurrence_id: "occ-1",
  agent_revision: "rev-7",
  context_id: "ctx-recorded",
  summary: "create_ticket accepts arbitrary strings instead of Priority enum.",
  edits: [],
  created_at: "2026-08-28T19:00:00Z",
};

function renderDetail(opts?: {
  insight?: InsightView;
  rootCauses?: RootCause[];
  contextsResponse?: { status: number; contexts?: StoredContext[] };
}) {
  const insight = opts?.insight ?? INSIGHT;
  const rootCauses = opts?.rootCauses ?? [];
  const contextsResponse = opts?.contextsResponse;

  vi.stubGlobal("fetch", (async (input: RequestInfo | URL) => {
    const url = typeof input === "string" ? input : input.toString();
    if (url.startsWith("/lha/contexts")) {
      if (!contextsResponse) {
        return new Response("{}", { status: 501 });
      }
      return new Response(
        JSON.stringify(
          contextsResponse.contexts
            ? { contexts: contextsResponse.contexts }
            : {},
        ),
        { status: contextsResponse.status },
      );
    }
    return new Response(
      JSON.stringify({ insight, occurrences: [], root_causes: rootCauses }),
      { status: 200 },
    );
  }) as typeof fetch);

  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: 0 } },
  });

  const rootRoute = createRootRoute();
  const indexRoute = createRoute({
    getParentRoute: () => rootRoute,
    path: "/",
    component: () => <InsightDetailView insightId={insight.insight_id} />,
  });
  const listRoute = createRoute({
    getParentRoute: () => rootRoute,
    path: "/insights",
    component: () => <div>All insights list</div>,
  });
  const chatRoute = createRoute({
    getParentRoute: () => rootRoute,
    path: "/c",
    component: () => <div>Chat</div>,
  });
  const history = createMemoryHistory({ initialEntries: ["/"] });
  const router = createRouter({
    routeTree: rootRoute.addChildren([indexRoute, listRoute, chatRoute]),
    history,
  });

  render(
    <QueryClientProvider client={queryClient}>
      <RouterProvider router={router} />
    </QueryClientProvider>,
  );
  return () => history.location.href;
}

const chatButton = () =>
  screen.findByRole("button", { name: /Diagnose insight in chat/ });
const chatOptionsButton = () =>
  screen.findByRole("button", {
    name: `Chat options for ${INSIGHT.insight_id}`,
  });

beforeEach(() => {
  resetConversationsForTest();
});

afterEach(() => {
  resetConversationsForTest();
  vi.unstubAllGlobals();
  document.querySelector('meta[name="aqua-viewer"]')?.remove();
});

describe("the detail header's Chat action", () => {
  it("opens /c with the question and insightId when no prior RCA chat or root cause exists", async () => {
    const href = renderDetail();

    await userEvent.click(await chatButton());

    await waitFor(() => expect(href()).toContain("/c?"));
    const params = new URL(href(), "http://localhost").searchParams;
    expect(params.get("q")).toBe(
      insightRcaPrompt(INSIGHT.insight_id, INSIGHT.label),
    );
    expect(params.get("insightId")).toBe(INSIGHT.insight_id);
  });

  it("guards against surprise RCA runs when a legacy root cause has no context_id", async () => {
    const href = renderDetail({
      insight: { ...INSIGHT, has_root_cause: true },
      rootCauses: [{ ...ROOT_CAUSE, context_id: "" }],
    });

    const panel = await screen.findByTestId("root-cause-panel");
    expect(
      within(panel).queryByRole("link", { name: /Open the chat/i }),
    ).toBeNull();

    await userEvent.click(await chatButton());
    expect(href()).toBe("/");
    expect(
      await screen.findByText("No chat session on record for this root cause."),
    ).toBeInTheDocument();

    await userEvent.click(
      await screen.findByRole("button", { name: "Start a new chat" }),
    );
    await waitFor(() => expect(href()).toContain("/c?"));
    const params = new URL(href(), "http://localhost").searchParams;
    expect(params.get("q")).toBe(
      insightRcaPrompt(INSIGHT.insight_id, INSIGHT.label),
    );
  });

  it("opens the newest RCA chat in 1 click on the primary button and lists all chats on the caret", async () => {
    rememberConversation(
      "ctx-followup",
      "Checking fallback branch",
      undefined,
      INSIGHT.insight_id,
    );
    const href = renderDetail({
      insight: {
        ...INSIGHT,
        has_root_cause: true,
        root_cause_context_ids: ["ctx-recorded"],
      },
      rootCauses: [ROOT_CAUSE],
      contextsResponse: {
        status: 200,
        contexts: [
          {
            context_id: "ctx-recorded",
            task_ids: ["t-1"],
            updated_at: "2026-08-28T19:00:00Z",
            title: "Initial priority RCA",
            first_message: "Diagnose insight",
          },
        ],
      },
    });

    const panel = await screen.findByTestId("root-cause-panel");
    const panelLink = await within(panel).findByRole("link", {
      name: /Open the chat/i,
    });
    expect(panelLink.getAttribute("href")).toContain("id=ctx-recorded");

    const primary = await chatButton();
    expect(primary).toHaveTextContent("Chat (2)");

    await userEvent.click(await chatOptionsButton());

    const list = await screen.findByRole("list", {
      name: "Existing chats for this insight",
    });
    expect(
      within(list).getByText("Checking fallback branch"),
    ).toBeInTheDocument();
    expect(within(list).getByText("Initial priority RCA")).toBeInTheDocument();
    expect(within(list).getByText("root cause")).toBeInTheDocument();

    await userEvent.click(within(list).getByText("Initial priority RCA"));

    await waitFor(() => expect(href()).toBe("/c?id=ctx-recorded"));
  });

  it("opens the latest chat directly in 1 click when clicking the primary split button", async () => {
    rememberConversation(
      "ctx-latest",
      "Latest RCA session",
      undefined,
      INSIGHT.insight_id,
    );
    const href = renderDetail();

    await userEvent.click(await chatButton());

    await waitFor(() => expect(href()).toBe("/c?id=ctx-latest"));
  });

  it("starts a fresh RCA chat when 'Start a new chat' is chosen from the caret menu", async () => {
    rememberConversation(
      "ctx-existing",
      "Earlier investigation",
      undefined,
      INSIGHT.insight_id,
    );
    const href = renderDetail();

    await userEvent.click(await chatOptionsButton());
    await userEvent.click(
      await screen.findByRole("button", { name: "Start a new chat" }),
    );

    await waitFor(() => expect(href()).toContain("/c?"));
    const params = new URL(href(), "http://localhost").searchParams;
    expect(params.get("id")).toBeNull();
    expect(params.get("q")).toBe(
      insightRcaPrompt(INSIGHT.insight_id, INSIGHT.label),
    );
    expect(params.get("insightId")).toBe(INSIGHT.insight_id);
  });

  it("omits deleted root-cause chats and shows the unlinked-root-cause popover instead of auto-starting", async () => {
    const href = renderDetail({
      insight: {
        ...INSIGHT,
        has_root_cause: true,
        root_cause_context_ids: ["ctx-deleted"],
      },
      rootCauses: [{ ...ROOT_CAUSE, context_id: "ctx-deleted" }],
      contextsResponse: { status: 200, contexts: [] },
    });

    const panel = await screen.findByTestId("root-cause-panel");
    await waitFor(() =>
      expect(
        within(panel).queryByRole("link", { name: /Open the chat/i }),
      ).toBeNull(),
    );

    await userEvent.click(await chatButton());
    expect(href()).toBe("/");
    expect(
      await screen.findByText("No chat session on record for this root cause."),
    ).toBeInTheDocument();

    await userEvent.click(
      await screen.findByRole("button", { name: "Start a new chat" }),
    );
    await waitFor(() => expect(href()).toContain("/c?"));
    expect(new URL(href(), "http://localhost").searchParams.get("q")).toBe(
      insightRcaPrompt(INSIGHT.insight_id, INSIGHT.label),
    );
  });

  it("is a named control reachable by keyboard", async () => {
    const href = renderDetail();

    const chat = await chatButton();
    chat.focus();
    expect(chat).toHaveFocus();
    expect(chat).toHaveAccessibleName(
      `Diagnose insight in chat: ${INSIGHT.label}`,
    );

    await userEvent.keyboard("{Enter}");

    await waitFor(() => expect(href()).toContain("/c?"));
  });

  it("is named in plain text when the heading renders code", async () => {
    renderDetail({
      insight: {
        ...INSIGHT,
        refined_label: "`create_ticket` declares no parameters",
      },
    });

    const heading = await screen.findByRole("heading", { level: 1 });
    expect(within(heading).getByText("create_ticket").tagName).toBe("CODE");

    const chat = await chatButton();
    expect(chat).toHaveAccessibleName(
      "Diagnose insight in chat: create_ticket declares no parameters",
    );
  });

  it("hides the Chat action, root-cause chat link, and /lha/contexts query in viewer mode", async () => {
    const meta = document.createElement("meta");
    meta.name = "aqua-viewer";
    meta.content = "1";
    document.head.prepend(meta);

    const asked: string[] = [];
    const baseFetch = globalThis.fetch;
    renderDetail({
      insight: {
        ...INSIGHT,
        has_root_cause: true,
        root_cause_context_ids: ["ctx-recorded"],
      },
      rootCauses: [ROOT_CAUSE],
      contextsResponse: {
        status: 200,
        contexts: [
          {
            context_id: "ctx-recorded",
            task_ids: ["t-1"],
            updated_at: "2026-08-28T19:00:00Z",
          },
        ],
      },
    });
    const stubbed = globalThis.fetch;
    vi.stubGlobal("fetch", (async (
      input: RequestInfo | URL,
      init?: RequestInit,
    ) => {
      asked.push(typeof input === "string" ? input : input.toString());
      return stubbed(input, init);
    }) as typeof fetch);
    void baseFetch;

    const panel = await screen.findByTestId("root-cause-panel");
    expect(
      within(panel).queryByRole("link", { name: /Open the chat/i }),
    ).toBeNull();
    expect(
      screen.queryByRole("button", { name: /Diagnose insight in chat/ }),
    ).toBeNull();
    expect(asked.filter((u) => u.startsWith("/lha/contexts"))).toEqual([]);
  });
});
