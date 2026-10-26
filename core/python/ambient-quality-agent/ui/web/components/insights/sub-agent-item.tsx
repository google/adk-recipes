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

import { Bot } from "lucide-react";
import type { InsightView } from "@/lib/aqua-api";
import { resolveSubAgent } from "@/lib/insight-agent";
import { mono } from "@/lib/typography";
import { cn } from "@/lib/utils";

/** Renders the sub-agent an insight is attributed to as a meta-line item.
 *
 * Renders nothing for the observed root agent, so a single-agent deployment
 * shows no attribution. Inherits text size and color from the parent meta
 * container.
 *
 * @param props.insight - The insight, or its `agent_id` and `agent_name`
 *   fields.
 * @returns The sub-agent element, or `null` when the insight has no distinct
 *   sub-agent.
 */
export function SubAgentItem({
  insight,
}: {
  insight: Pick<InsightView, "agent_id" | "agent_name">;
}) {
  const subAgent = resolveSubAgent(insight);
  if (!subAgent) return null;
  return (
    <span
      data-testid="insight-agent"
      title="Sub-agent this defect is attributed to"
      className="inline-flex min-w-0 items-center gap-1"
    >
      <Bot className="h-3 w-3 shrink-0" aria-hidden="true" />
      {/* `title` is not read reliably by screen readers or shown on touch. */}
      <span className="sr-only">Sub-agent: </span>
      <span className={cn(mono, "truncate")}>{subAgent}</span>
    </span>
  );
}
