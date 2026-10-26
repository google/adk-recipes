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

import type { InsightView } from "./aqua-api";

/** Returns the sub-agent to display as an insight's attribution.
 *
 * The tab bar and insights page header already show the observed root agent, so
 * repeating it on every insight of a single-agent deployment adds noise.
 *
 * @param insight - The insight, or its `agent_id` and `agent_name` fields.
 * @returns The sub-agent name, or `undefined` when the insight belongs to the
 *   observed root agent or is unattributed.
 */
export function resolveSubAgent(
  insight: Pick<InsightView, "agent_id" | "agent_name">,
): string | undefined {
  const agentId = insight.agent_id;
  if (!agentId || agentId === insight.agent_name) return undefined;
  return agentId;
}
