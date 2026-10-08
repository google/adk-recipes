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

import { useQuery } from "@tanstack/react-query";
import { Link } from "@tanstack/react-router";
import { Stethoscope, TriangleAlert } from "lucide-react";
import {
  insightsQuery,
  isDiagnosed,
  runsQuery,
  type InsightView,
} from "@/lib/aqua-api";
import { evaluatedTraces } from "@/lib/funnel";
import { cn } from "@/lib/utils";
import { SidebarSection } from "@/components/chat/sidebar-section";
import {
  SIDEBAR_META_CLASS,
  SIDEBAR_ROW_ACTIVE_CLASS,
  SIDEBAR_ROW_CLASS,
  SidebarAllLink,
  SidebarError,
  SidebarNote,
  SidebarSkeleton,
  formatCount,
} from "@/components/chat/sidebar-states";
import {
  RAIL_SCROLL_CLASS,
  useRailScroll,
} from "@/components/chat/use-rail-scroll";
import { DiagnoseInChatButton } from "./diagnose-in-chat-button";

/** How many issues the rail shows. */
const RAIL_SIZE = 10;

const topIssuesQuery = () =>
  insightsQuery({ orderBy: "impact", pageSize: RAIL_SIZE });

/** The worst issues, in the sidebar's spare height, as a sidebar section.
 *
 * A developer arriving from an alert should not have to find the Insights tab
 * to learn what is broken. A handful, not all of them: this is a pointer to
 * the tab, not a copy of it. The header counts all of them.
 *
 * The engine picks them: ranked and cut there, these are the worst issues
 * overall rather than the worst of whatever one page held.
 */
export function TopIssuesRail() {
  const query = useQuery(topIssuesQuery());
  // An empty or error line takes only its own height.
  const hasRows = query.data ? query.data.insights.length > 0 : !query.isError;
  return (
    <SidebarSection
      title="Top insights"
      icon={TriangleAlert}
      storageKey="aqua.sidebar.issues"
      count={query.data?.total}
      fill={hasRows}
      rightSlot={<SidebarAllLink to="/insights" label="All insights" />}
    >
      <TopIssuesList />
    </SidebarSection>
  );
}

function TopIssuesList() {
  // react-query keeps `data` through a failed refetch, so the rows stay up and
  // the error line is only for a list that never loaded.
  const { data, isError, refetch } = useQuery(topIssuesQuery());
  const top = data?.insights ?? [];
  // Only an empty page needs the runs, to tell "nothing failing" from "nothing
  // analysed yet"; the Recent investigations section shares the query.
  const runs = useQuery({
    ...runsQuery(),
    enabled: data !== undefined && top.length === 0,
  });
  const scrollRef = useRailScroll<HTMLUListElement>("issues");

  if (!data) {
    return isError ? (
      <SidebarError onRetry={() => void refetch()} />
    ) : (
      <SidebarSkeleton label="Loading top insights" rowClassName="h-12" />
    );
  }
  if (top.length === 0) {
    const evaluated = runs.data?.some(
      (run) => evaluatedTraces(run.counters ?? {}) > 0,
    );
    return (
      <SidebarNote>
        {evaluated ? "Nothing failing right now." : "No insights yet."}
      </SidebarNote>
    );
  }

  return (
    <ul
      ref={scrollRef}
      className={cn(RAIL_SCROLL_CLASS, "flex flex-col gap-0.5 pb-2")}
    >
      {top.map((insight) => (
        <IssueRow key={insight.insight_id} insight={insight} />
      ))}
    </ul>
  );
}

function IssueRow({ insight }: { insight: InsightView }) {
  const title =
    insight.diagnosis.trim() || insight.label.trim() || "Untitled insight";
  return (
    // The Diagnose button is the row link's sibling, laid over the end of the
    // short meta line so the title keeps the full width: inside the link it
    // would be an interactive control nested in another.
    <li className="group/item relative">
      <Link
        to="/insights/$insightId"
        params={{ insightId: insight.insight_id }}
        className={cn(SIDEBAR_ROW_CLASS, "group flex flex-col gap-0.5 py-1.5")}
        activeProps={{ className: SIDEBAR_ROW_ACTIVE_CLASS }}
      >
        <span
          dir="auto"
          title={title}
          className="line-clamp-2 min-w-0 break-words"
        >
          {title}
        </span>
        <span className={cn(SIDEBAR_META_CLASS, "flex items-center gap-1")}>
          {insight.has_root_cause && (
            <span
              data-testid="root-cause-icon"
              role="img"
              aria-label="An RCA turn recorded a root cause for this insight"
              title="An RCA turn recorded a root cause for this insight"
              className="flex shrink-0 items-center"
            >
              <Stethoscope className="size-3" />
            </span>
          )}
          {/* Trajectories first: they are what the rail is ranked by. No percentage --
              nothing measures one. A bare 0% would read as "certainly nothing"
              rather than "not investigated yet". */}
          <span>
            {formatCount(insight.trace_count)}{" "}
            {insight.trace_count === 1 ? "trajectory" : "trajectories"}
            {!isDiagnosed(insight) && " · undiagnosed"}
          </span>
        </span>
      </Link>
      {/* Icon-only for the compact rail; visible on hover and keyboard focus. */}
      <DiagnoseInChatButton
        insightId={insight.insight_id}
        label={insight.label}
        ariaLabel={`Diagnose insight in chat: ${title}`}
        iconOnly
        className={cn(
          "absolute bottom-1 right-1 h-6 w-6 p-0 [&_svg]:size-3.5",
          "text-muted-foreground hover:text-foreground",
          "opacity-0 focus-visible:opacity-100 group-hover/item:opacity-100",
        )}
      />
    </li>
  );
}
