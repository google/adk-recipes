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

/** Which of AQuA's attached agents the dashboard shows.
 *
 * The selection is the `agent` search parameter, which the root route keeps
 * on every navigation, so a deep link to an insight or a run opens it for the
 * agent it belongs to. Without the parameter the dashboard shows the agent
 * AQuA answers a request that names none: its default agent.
 *
 * Read through a context rather than from the router in each component, so
 * that a component rendered outside the router, as most unit tests render
 * them, reads the default agent instead of failing.
 */

import { createContext, useContext, useMemo, type ReactNode } from "react";
import { useQuery } from "@tanstack/react-query";
import { useSearch } from "@tanstack/react-router";
import { type AgentKey, type AttachedAgent, agentsQuery } from "./aqua-api";

/** The search parameter the selected agent's key is kept in. */
export const AGENT_SEARCH_PARAM = "agent";

export interface AgentSelection {
  /** The key in the URL, or undefined when the default agent is shown. What
   *  every read and write sends, so none waits for the agent list. */
  selectedKey: AgentKey;
  /** The agent the dashboard is showing: the selected one, else the default
   *  one once the agent list has said which that is. */
  shownKey: AgentKey;
  /** The agent a request that selects none gets, once known. */
  defaultKey: AgentKey;
  /** Every attached agent, empty until the list has loaded. */
  agents: AttachedAgent[];
}

const AgentSelectionContext = createContext<AgentSelection>({
  selectedKey: undefined,
  shownKey: undefined,
  defaultKey: undefined,
  agents: [],
});

/**
 * Reads the agent selection from a search object.
 *
 * @param search - The router's search parameters.
 * @returns The selected key, or undefined when none is selected.
 */
export function parseAgentSearch(search: Record<string, unknown>): AgentKey {
  const value = search[AGENT_SEARCH_PARAM];
  return typeof value === "string" && value.trim() ? value.trim() : undefined;
}

export function AgentSelectionProvider({ children }: { children: ReactNode }) {
  const selectedKey = useSearch({
    strict: false,
    select: (search) => parseAgentSearch(search as Record<string, unknown>),
  });
  const { data } = useQuery(agentsQuery());
  const value = useMemo<AgentSelection>(() => {
    const defaultKey = data?.default_agent_key ?? undefined;
    return {
      selectedKey,
      shownKey: selectedKey ?? defaultKey,
      defaultKey,
      agents: data?.agents ?? [],
    };
  }, [selectedKey, data]);
  return (
    <AgentSelectionContext.Provider value={value}>
      {children}
    </AgentSelectionContext.Provider>
  );
}

/** The whole selection, for the selector and the chat. */
export function useAgentSelection(): AgentSelection {
  return useContext(AgentSelectionContext);
}

/** The key every dashboard read and write sends: the selected agent's. */
export function useAgentKey(): AgentKey {
  return useContext(AgentSelectionContext).selectedKey;
}

/**
 * Adds the selected agent to a link into the dashboard that the router does
 * not build, such as a permalink or a link in a chat reply, so that it opens
 * on the same agent.
 *
 * @param href - A path into the dashboard, with any query and fragment.
 * @param agentKey - The selected agent, or undefined for the default one.
 * @returns The path with the agent in its query, unless it names one already.
 */
export function buildAgentHref(href: string, agentKey: AgentKey): string {
  if (!agentKey) return href;
  const url = new URL(href, "http://dashboard.invalid");
  if (url.searchParams.has(AGENT_SEARCH_PARAM)) return href;
  url.searchParams.set(AGENT_SEARCH_PARAM, agentKey);
  return `${url.pathname}${url.search}${url.hash}`;
}

/**
 * Checks whether a conversation belongs to the agent the dashboard shows.
 *
 * A conversation whose messages named no agent carries no key. The agent
 * answers those for its default agent, so they are listed with it.
 *
 * @param conversationKey - The agent the conversation recorded, if any.
 * @param selection - The dashboard's agent selection.
 * @returns True when the conversation is about the shown agent.
 */
export function isConversationShown(
  conversationKey: AgentKey,
  selection: Pick<AgentSelection, "shownKey" | "defaultKey">,
): boolean {
  return (conversationKey ?? selection.defaultKey) === selection.shownKey;
}
