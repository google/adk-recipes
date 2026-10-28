"use client";
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

import { useState, type ReactNode } from "react";
import { useLocation, useNavigate } from "@tanstack/react-router";
import { Check, ChevronDown } from "lucide-react";
import type { AttachedAgent } from "@/lib/aqua-api";
import { useAgentSelection } from "@/lib/agent-selection";
import {
  Popover,
  PopoverContent,
  PopoverTrigger,
} from "@/components/ui/popover";
import { mono, textStyle } from "@/lib/typography";
import { cn } from "@/lib/utils";

/** Where switching agents lands, for each part of the dashboard.
 *
 * A list stays on its list, without its filters: a run or a day filter names
 * the previous agent's runs. A detail page goes to the list it came from,
 * since the insight or run it shows belongs to the previous agent, and the
 * chat starts a new conversation.
 *
 * @param pathname - The page open when the agent is switched.
 * @returns The path to open for the next agent.
 */
export function resolveAgentSwitchPath(
  pathname: string,
): "/" | "/insights" | "/investigations" | "/config" | "/c" {
  if (pathname.startsWith("/insights")) return "/insights";
  if (pathname.startsWith("/investigations")) return "/investigations";
  if (pathname.startsWith("/config")) return "/config";
  if (pathname === "/c" || pathname.startsWith("/c/")) return "/c";
  return "/";
}

/**
 * Describes where an attached agent runs, to tell apart one agent deployed
 * into several projects.
 *
 * @param agent - An attached agent.
 * @returns Its project, or "AQuA's project" for one keyed by its bare name.
 */
export function formatAgentProject(agent: AttachedAgent): string {
  return agent.project_id ?? "AQuA's project";
}

/** The agent badge, and the menu that switches between the attached agents.
 *
 * With one agent attached, or before the list has loaded, this is the badge
 * alone: there is nothing to choose between.
 */
export function AgentSelector({
  fallbackName,
  children,
}: {
  /** The agent's name when the list does not have it: before it loads. */
  fallbackName: string;
  /** Drawn before the name, inside the badge: the health dot. */
  children?: ReactNode;
}) {
  const { agents, shownKey, defaultKey } = useAgentSelection();
  const navigate = useNavigate();
  const pathname = useLocation({ select: (location) => location.pathname });
  const [open, setOpen] = useState(false);
  const shown = agents.find((agent) => agent.agent_key === shownKey);
  const name = shown?.agent_name || shownKey || fallbackName;
  if (!name) return null;

  const badge = (
    <>
      {children}
      <span className={cn(mono, "text-foreground font-medium")}>{name}</span>
      {shown?.project_id && (
        <span className={cn(mono, "text-muted-foreground")}>
          {shown.project_id}
        </span>
      )}
    </>
  );
  const badgeClass = cn(
    textStyle.meta,
    "hidden sm:flex items-center gap-1.5 rounded-full bg-muted/40 px-2.5 py-0.5 border border-border/40",
  );

  if (agents.length < 2) {
    return (
      <div data-testid="agent-freshness-badge" className={badgeClass}>
        {badge}
      </div>
    );
  }

  const select = (agentKey: string) => {
    setOpen(false);
    if (agentKey === shownKey) return;
    void navigate({
      to: resolveAgentSwitchPath(pathname),
      // The default agent drops the parameter rather than naming itself, so
      // its pages share one URL, one cache entry and one last chat with a
      // plain load of the dashboard.
      search: { agent: agentKey === defaultKey ? undefined : agentKey },
    });
  };

  return (
    <Popover open={open} onOpenChange={setOpen}>
      <PopoverTrigger asChild>
        <button
          type="button"
          data-testid="agent-freshness-badge"
          aria-label={`Agent: ${name}. Switch agent`}
          className={cn(badgeClass, "hover:border-foreground/30")}
        >
          {badge}
          <ChevronDown className="h-3 w-3 text-muted-foreground" />
        </button>
      </PopoverTrigger>
      <PopoverContent align="start" className="w-72 p-1">
        <ul aria-label="Attached agents" className="flex flex-col">
          {agents.map((agent) => {
            const current = agent.agent_key === shownKey;
            return (
              <li key={agent.agent_key}>
                <button
                  type="button"
                  disabled={Boolean(agent.error)}
                  title={agent.error ?? agent.agent_key}
                  aria-current={current ? "true" : undefined}
                  onClick={() => select(agent.agent_key)}
                  className={cn(
                    "flex w-full items-start gap-2 rounded-md px-2 py-1.5 text-left",
                    "hover:bg-muted disabled:cursor-not-allowed disabled:opacity-60",
                  )}
                >
                  <Check
                    className={cn(
                      "mt-0.5 h-3.5 w-3.5 shrink-0",
                      current ? "opacity-100" : "opacity-0",
                    )}
                  />
                  <span className="flex min-w-0 flex-col">
                    <span className={cn(mono, textStyle.body, "truncate")}>
                      {agent.agent_name || agent.agent_key}
                    </span>
                    <span className={cn(textStyle.meta, "truncate")}>
                      {agent.error
                        ? "Cannot be read"
                        : formatAgentProject(agent)}
                      {agent.default && " · default"}
                    </span>
                  </span>
                </button>
              </li>
            );
          })}
        </ul>
      </PopoverContent>
    </Popover>
  );
}
