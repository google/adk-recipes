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

import type { QueryClient } from "@tanstack/react-query";
import {
  type AgentKey,
  buildAgentScope,
  type InsightView,
  type InsightsPage,
  type InvestigationListItem,
} from "./aqua-api";

/** How the command palette finds insights and investigations.
 *
 * Text is searched over every insight on the server; what the dashboard has
 * already read is matched as well, so a result in hand shows before the
 * request returns. Insight and investigation ids share one format, so an id
 * alone does not say which of the two it names: a pasted id that matches
 * nothing is looked up as both.
 */

/** Returns true if the query could be an id: one token of letters, digits,
 *  `-` or `_`, with a digit in it, and long enough to be the 8-character short
 *  form the dashboard shows. A word such as "planner" is not one, so typing it
 *  does not send a lookup. */
export function isIdQuery(query: string): boolean {
  const q = query.trim();
  return q.length >= 8 && /^[\w-]+$/.test(q) && /\d/.test(q);
}

/** Returns every insight in the cached insight lists for the agent, each once.
 *
 * @param queryClient - The dashboard's query client.
 * @param agentKey - The selected agent.
 * @returns The insights, in the order the lists hold them.
 */
export function listCachedInsights(
  queryClient: QueryClient,
  agentKey: AgentKey,
): InsightView[] {
  const byId = new Map<string, InsightView>();
  for (const [, page] of queryClient.getQueriesData<InsightsPage>({
    queryKey: [...buildAgentScope(agentKey), "insights"],
  })) {
    for (const insight of page?.insights ?? []) {
      if (!byId.has(insight.insight_id)) byId.set(insight.insight_id, insight);
    }
  }
  return [...byId.values()];
}

/** Returns every investigation in the cached investigation lists for the
 *  agent, each once.
 *
 * @param queryClient - The dashboard's query client.
 * @param agentKey - The selected agent.
 * @returns The investigations, in the order the lists hold them.
 */
export function listCachedInvestigations(
  queryClient: QueryClient,
  agentKey: AgentKey,
): InvestigationListItem[] {
  const byId = new Map<string, InvestigationListItem>();
  for (const [, list] of queryClient.getQueriesData<InvestigationListItem[]>({
    queryKey: [...buildAgentScope(agentKey), "investigations"],
  })) {
    for (const investigation of list ?? []) {
      if (!byId.has(investigation.investigation_id)) {
        byId.set(investigation.investigation_id, investigation);
      }
    }
  }
  return [...byId.values()];
}

/** Returns the insights a query matches, ids that start with it first.
 *
 * @param insights - The insights to search.
 * @param query - What the user typed; matched case-insensitively.
 * @param limit - The most to return.
 * @returns Insights whose id starts with the query, then those whose label,
 *   refined label or diagnosis contains it.
 */
export function listMatchingInsights(
  insights: readonly InsightView[],
  query: string,
  limit: number,
): InsightView[] {
  const q = query.trim().toLowerCase();
  if (!q) return [];
  const byId = insights.filter((i) => i.insight_id.toLowerCase().startsWith(q));
  const byText = insights.filter(
    (i) =>
      !i.insight_id.toLowerCase().startsWith(q) &&
      [i.label, i.refined_label, i.diagnosis].some((text) =>
        text?.toLowerCase().includes(q),
      ),
  );
  return [...byId, ...byText].slice(0, limit);
}

/** Returns the investigations whose id starts with the query.
 *
 * @param investigations - The investigations to search.
 * @param query - What the user typed; matched case-insensitively.
 * @param limit - The most to return.
 * @returns The matching investigations, in the order given.
 */
export function listMatchingInvestigations(
  investigations: readonly InvestigationListItem[],
  query: string,
  limit: number,
): InvestigationListItem[] {
  const q = query.trim().toLowerCase();
  if (!q) return [];
  return investigations
    .filter((i) => i.investigation_id.toLowerCase().startsWith(q))
    .slice(0, limit);
}

/** Returns the loaded matches followed by the server's, each insight once.
 *
 * @param loaded - Matches among the insights the dashboard has read.
 * @param searched - The server's matches over every insight.
 * @param limit - The most to return.
 * @returns The combined matches, loaded ones first.
 */
export function mergeInsightMatches(
  loaded: readonly InsightView[],
  searched: readonly InsightView[],
  limit: number,
): InsightView[] {
  const seen = new Set(loaded.map((i) => i.insight_id));
  return [...loaded, ...searched.filter((i) => !seen.has(i.insight_id))].slice(
    0,
    limit,
  );
}
