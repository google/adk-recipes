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

import {
  createRootRoute,
  HeadContent,
  Outlet,
  retainSearchParams,
} from "@tanstack/react-router";
import { QueryProvider } from "@/app/providers";
import { TabBar } from "@/components/nav/tab-bar";
import { TourOverlay } from "@/components/tour/tour-overlay";
import {
  AGENT_SEARCH_PARAM,
  AgentSelectionProvider,
  parseAgentSearch,
  useAgentKey,
} from "@/lib/agent-selection";
import { usePrefetchAqua } from "@/lib/aqua-prefetch";
import { TooltipProvider } from "@/components/ui/tooltip";
import "../fonts.css";
import "@/app/globals.css";

function RootComponent() {
  return (
    <QueryProvider>
      <AgentSelectionProvider>
        <PrefetchOnBoot />
        <TooltipProvider delayDuration={300}>
          <HeadContent />
          <div className="flex h-dvh min-h-0 flex-col">
            <TabBar />
            <div className="min-h-0 flex-1">
              <AgentScopedOutlet />
            </div>
          </div>
          <TourOverlay />
        </TooltipProvider>
      </AgentSelectionProvider>
    </QueryProvider>
  );
}

/** The page, mounted afresh for each agent. A draft goal, a filter or a chat
 *  in progress belongs to the agent it was started on, so none of it may
 *  carry over to the next one. */
function AgentScopedOutlet() {
  const agentKey = useAgentKey();
  return <Outlet key={agentKey ?? ""} />;
}

/** Inside QueryProvider and AgentSelectionProvider so it can reach the client
 *  and the selected agent; renders nothing. */
function PrefetchOnBoot() {
  usePrefetchAqua();
  return null;
}

export const Route = createRootRoute({
  // The selected agent, on every route. Kept by every navigation that does not
  // set it, so a link or a `navigate` written without it stays on the agent.
  // Absent rather than undefined when none is selected: navigation validates
  // the next search inside `retainSearchParams`, which keeps only a key the
  // next search does not have.
  validateSearch: (search: Record<string, unknown>): { agent?: string } => {
    const agent = parseAgentSearch(search);
    return agent ? { agent } : {};
  },
  search: { middlewares: [retainSearchParams([AGENT_SEARCH_PARAM])] },
  head: () => ({
    meta: [
      { title: "AQuA" },
      {
        name: "description",
        content: "A self-improving coding agent built on Google's ADK.",
      },
    ],
  }),
  component: RootComponent,
});
