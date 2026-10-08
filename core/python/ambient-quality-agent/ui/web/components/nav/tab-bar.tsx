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

import { Link } from "@tanstack/react-router";
import { useQuery } from "@tanstack/react-query";
import { Compass, PanelLeft, Settings } from "lucide-react";
import {
  effectiveConfigQuery,
  healthQuery,
  insightsQuery,
  runsQuery,
} from "@/lib/aqua-api";
import { useRailState } from "@/lib/rail-state";
import { startTour } from "@/lib/tour-state";
import { AppearancePopover } from "@/components/chat/appearance-popover";
import { FeedbackPopover } from "@/components/chat/feedback-popover";
import { Button } from "@/components/ui/button";
import { mono, textStyle } from "@/lib/typography";
import { cn } from "@/lib/utils";

/** Brand and global controls.
 *
 * Navigation lives in the sidebar, which already lists conversations, issues
 * and investigations -- tabs here would be a second place to go to the same
 * three destinations, and the wordmark appeared twice on the chat route.
 */
export function TabBar() {
  const { data: runs } = useQuery(runsQuery());
  const { data: health } = useQuery(healthQuery());
  // One row: all this needs off the list is the agent name it carries, for
  // when no run has reported one.
  const { data: insightsPage } = useQuery(insightsQuery({ pageSize: 1 }));
  // Before any run or insight, the configured agent is the only name there is.
  const { data: effectiveConfig } = useQuery(effectiveConfigQuery());
  const { collapsed: railCollapsed, toggle: toggleRail } = useRailState();
  const latestRun = runs?.[0];
  const configuredName = effectiveConfig?.config?.observed_agent_name;
  const agentName =
    latestRun?.observed_agent_name ||
    insightsPage?.insights?.[0]?.agent_name ||
    (typeof configuredName === "string" ? configuredName : "");

  return (
    <div className="flex h-10 shrink-0 items-center justify-between border-b bg-background px-3">
      <div className="flex items-center gap-3">
        <Link to="/" className={textStyle.itemTitle}>
          AQuA
        </Link>
        {/* A collapsed rail has nothing left to drag, so this is the only way
            back. It belongs in the bar, beside the wordmark: floating it over
            the page puts it on top of that wordmark, because nothing between
            the page and the viewport is positioned. */}
        {railCollapsed && toggleRail && (
          <Button
            variant="ghost"
            size="icon"
            className="-ml-1 h-7 w-7 text-muted-foreground hover:text-foreground"
            onClick={toggleRail}
            aria-label="Show chats"
            title="Show chats"
            // The rail hands focus here after collapsing or closing its drawer.
            data-rail-toggle=""
          >
            <PanelLeft className="h-4 w-4" />
          </Button>
        )}
        {agentName && (
          <div
            data-testid="agent-freshness-badge"
            className={cn(
              textStyle.meta,
              "hidden sm:flex items-center gap-1.5 rounded-full bg-muted/40 px-2.5 py-0.5 border border-border/40",
            )}
          >
            {/* The dot is on every route and outlives the homepage strip, so
                it cannot be decorative: a green pulse over a scheduler that
                has never fired is the loudest lie on the screen. */}
            <span
              data-testid="agent-health-dot"
              title={health?.reason ?? "health unknown"}
              className={cn(
                "h-1.5 w-1.5 rounded-full",
                health?.verdict === "watching" &&
                  "bg-emerald-500 animate-pulse",
                (health?.verdict === "overdue" ||
                  health?.verdict === "never" ||
                  health?.verdict === "disabled") &&
                  "bg-amber-500",
                health?.verdict === "failing" && "bg-red-500",
                !health?.verdict && "bg-muted-foreground/40",
              )}
            />
            {/* Freshness lives in the homepage stat bar, from the health read.
                Two sources for one fact disagreed on screen: this said "3d
                ago" beside the bar's "1d ago". */}
            <span className={cn(mono, "text-foreground font-medium")}>
              {agentName}
            </span>
          </div>
        )}
      </div>
      <div className="ml-auto flex items-center gap-1">
        <Button
          asChild
          variant="ghost"
          size="icon"
          className="h-8 w-8 text-muted-foreground hover:text-foreground"
          title="Configuration"
        >
          <Link to="/config" aria-label="Configuration">
            <Settings className="h-4 w-4" />
          </Link>
        </Button>
        <Button
          variant="ghost"
          size="icon"
          className="h-8 w-8 text-muted-foreground hover:text-foreground"
          onClick={startTour}
          aria-label="Take the tour"
          title="Take the tour"
          data-tour="tour-button"
        >
          <Compass className="h-4 w-4" />
        </Button>
        <AppearancePopover />
        <FeedbackPopover />
      </div>
    </div>
  );
}
