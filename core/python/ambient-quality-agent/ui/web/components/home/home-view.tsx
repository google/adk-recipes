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

import { useState } from "react";
import { Link, useNavigate } from "@tanstack/react-router";
import { useQuery } from "@tanstack/react-query";
import { ArrowRight, ArrowUp } from "lucide-react";
import {
  dailyQuery,
  effectiveConfigQuery,
  type InsightOrder,
  insightsQuery,
  runsQuery,
  statsQuery,
} from "@/lib/aqua-api";
import { FailureNote } from "@/components/ui/failure-note";
import { FilterTabs } from "@/components/ui/filter-tabs";
import { HelpPopover } from "@/components/ui/help-popover";
import { RunsTable } from "@/components/investigations/runs-table";
import { RunInvestigationButton } from "@/components/run-investigation-button";
import { Funnel } from "@/components/stats/funnel";
import { InsightTrend } from "@/components/stats/insight-trend";
import { TracesPerDay } from "@/components/stats/traces-per-day";
import { DASHBOARD_DAYS, HORIZON_LABEL } from "@/lib/day-buckets";
import type { ListPage } from "@/lib/funnel";
import { buildTrend, resolvedNote } from "@/lib/insight-trend";
import { newestFirst } from "@/lib/runstate";
import { cn, formatSentenceCase } from "@/lib/utils";
import { PageHeader } from "@/components/nav/page-header";
import { link, textStyle } from "@/lib/typography";
import { StatBar } from "./stat-bar";

/** How many investigations the Recent investigations list shows, of the
 *  capped page `runsQuery` returns. Few, so Home stays short; "View all"
 *  opens the rest. */
const RECENT_RUNS = 5;

/** How many issues the homepage strip shows. */
const TOP_ISSUES = 5;

/**
 * The views the insights strip offers, each one the engine's own ordering or
 * status filter, so the strip never ranks a page after it arrives.
 */
const STRIP_VIEWS = {
  top: {
    title: "Top insights",
    subtitle: "Most trajectories affected first",
    query: { orderBy: "impact" },
  },
  latest: {
    title: "Latest insights",
    subtitle: "Last seen first",
    query: { orderBy: "recent" },
  },
  recurring: {
    title: "Recurring insights",
    subtitle: "Seen again by a later investigation, most trajectories first",
    query: { orderBy: "impact", status: "RECURRING" },
  },
} as const satisfies Record<
  string,
  {
    title: string;
    subtitle: string;
    query: { orderBy: InsightOrder; status?: string };
  }
>;
type StripView = keyof typeof STRIP_VIEWS;
const STRIP_VIEW_NAMES = Object.keys(STRIP_VIEWS) as StripView[];

/** What each chart draws, restating how it is built: per-day rows are bucketed
 *  by `DATE(window_end)` (`BigQueryInvestigationStore.counters_by_day`), and a
 *  resolution is an auto-resolve (`resolve_stale_insights`), dated by its
 *  `resolved_at`. The hatched days use the legends' words. Charts only: the
 *  funnel's stages carry their own, in `STAGE_HELP`. */
export const CHART_HELP = {
  activity:
    "Each column is one day's trajectories sent for evaluation, from the " +
    "investigations whose window ended that day (UTC), filled with failed on " +
    "top of passed. Eval service errors and trajectories with no outcome are " +
    "not drawn; they leave the top of the column unfilled. Hatched: no " +
    "trajectories evaluated.",
  /** `autoResolveDays` is the deployment's window, or null to name it generically. */
  insightTrend: (autoResolveDays: number | null) =>
    "New counts the insights created by the investigations whose window ended " +
    "that day (UTC). Resolved counts the insights auto-resolved that day, " +
    `after going unseen for ${autoResolveDays ? `${autoResolveDays} days` : "the auto-resolve window"}. ` +
    "Hatched: no investigations.",
};

function Section({
  title,
  subtitle,
  help,
  action,
  tour,
  className,
  children,
}: {
  title: string;
  subtitle: string;
  /** What the section draws, behind an (i) beside the title. */
  help?: string;
  action?: React.ReactNode;
  /** The guided tour's name for the section, when a step points at it. */
  tour?: string;
  className?: string;
  children: React.ReactNode;
}) {
  return (
    <section
      data-tour={tour}
      className={cn("min-w-0 overflow-hidden", className)}
    >
      {/* The subtitle sits below the row, not beside the action: in a narrow
          column the action would squeeze it onto three lines. */}
      {/* Wraps only as a last resort, and then the action moves under the
          title whole rather than squeezing the title onto two lines. */}
      <div className="flex flex-wrap items-center justify-between gap-x-4 gap-y-1">
        <div className="flex items-center gap-1.5">
          <h2 className={cn(textStyle.sectionTitle, "whitespace-nowrap")}>
            {title}
          </h2>
          {help && (
            <HelpPopover topic={`the ${title} chart`}>{help}</HelpPopover>
          )}
        </div>
        {action}
      </div>
      <p className={cn(textStyle.description, "mt-0.5")}>{subtitle}</p>
      <div className="mt-4 min-w-0 overflow-hidden">{children}</div>
    </section>
  );
}

/** A section's way to the page that lists what it shows. The ring is inset
 *  because the section clips overflow, which would cut an outline off. */
function SectionLink({
  to,
  search,
  children,
}: {
  to: ListPage;
  search?: { status?: "recurring" };
  children: React.ReactNode;
}) {
  return (
    <Link
      to={to}
      search={search}
      className={cn(
        textStyle.body,
        link.standalone,
        "flex shrink-0 items-center gap-1.5 text-primary focus-visible:ring-inset",
      )}
    >
      {children} <ArrowRight className="h-3.5 w-3.5" aria-hidden="true" />
    </Link>
  );
}

/** `insights_auto_resolve_days` off the effective config, or null when it is
 *  unset or zero — both of which mean auto-resolution is disabled, and so that
 *  no resolution date can be inferred for a row that never recorded one. */
function autoResolveWindow(
  config: Record<string, unknown> | undefined,
): number | null {
  const days = Number(config?.insights_auto_resolve_days);
  return Number.isFinite(days) && days > 0 ? days : null;
}

function Empty({ children }: { children: React.ReactNode }) {
  return (
    <p className={cn(textStyle.description, "py-8 text-center")}>{children}</p>
  );
}

/**
 * Loading, empty and failed are three states, not one.
 *
 * Rendering "nothing found yet" while a query is still in flight is the
 * confident-absence bug this codebase keeps shipping, and these lists take a
 * round trip through the engine.
 */
function Pane({
  query,
  empty,
  isEmpty,
  what,
  children,
}: {
  query: { isLoading: boolean; error: unknown };
  empty: string;
  isEmpty: boolean;
  /** What this pane holds, for the failure line: "the pipeline figures". */
  what: string;
  children: React.ReactNode;
}) {
  if (query.isLoading) {
    return <div className="h-24 animate-pulse rounded-lg bg-muted/20" />;
  }
  if (query.error) {
    // Compact: `StatBar` sits above every one of these and explains in full.
    return (
      <FailureNote
        lead={`Couldn't load ${what}`}
        error={query.error}
        compact
        className="py-4"
      />
    );
  }
  if (isEmpty) return <Empty>{empty}</Empty>;
  return <>{children}</>;
}

function AskBar() {
  const [text, setText] = useState("");
  const navigate = useNavigate();
  const ask = (q: string) => {
    const question = q.trim();
    if (!question) return;
    // The chat owns sending; the dashboard only carries the question over.
    void navigate({ to: "/c", search: { q: question } });
  };

  return (
    <div data-tour="ask">
      <form
        onSubmit={(e) => {
          e.preventDefault();
          ask(text);
        }}
        className="flex items-center gap-2 rounded-xl border bg-background px-4 py-2.5"
      >
        <input
          value={text}
          onChange={(e) => setText(e.target.value)}
          placeholder="Ask about the quality insights AQuA found…"
          aria-label="Ask AQuA"
          className={cn(
            textStyle.body,
            "min-w-0 flex-1 bg-transparent outline-none placeholder:text-muted-foreground",
          )}
        />
        <button
          type="submit"
          aria-label="Send"
          disabled={!text.trim()}
          className="grid h-7 w-7 shrink-0 place-items-center rounded-full bg-muted disabled:opacity-40"
        >
          <ArrowUp className="h-3.5 w-3.5" />
        </button>
      </form>
      <div className="mt-2 flex flex-wrap gap-2">
        {[
          "What are the worst insights right now?",
          "Run a new custom investigation",
          "Investigate the failures in the observed agent",
          "What changed since the last investigation?",
        ].map((s) => (
          <button
            key={s}
            type="button"
            onClick={() => ask(s)}
            className={cn(
              textStyle.meta,
              "rounded-full border px-3 py-1 hover:bg-muted/50",
            )}
          >
            {s}
          </button>
        ))}
      </div>
    </div>
  );
}

/**
 * The homepage: what AQuA knows, at a glance, with a way to ask about it.
 *
 * Chat is still the product -- the ask bar hands straight to it -- but arriving
 * at an empty prompt made you ask before you could see anything.
 */
export function HomeView() {
  const stats = useQuery(statsQuery());
  const [stripView, setStripView] = useState<StripView>("top");
  const view = STRIP_VIEWS[stripView];
  // Five, ordered and filtered by the engine: the strip shows the first five
  // of the whole list, not the first of a page it happened to be sent.
  const insights = useQuery(
    insightsQuery({ ...view.query, pageSize: TOP_ISSUES }),
  );
  const runs = useQuery(runsQuery());
  const daily = useQuery(dailyQuery());
  // Resolved issues are fetched on their own rather than filtered out of the
  // list above: that list is impact-ordered and five rows long, so the resolved
  // ones it happens to carry are not the resolved ones, and the chart would be
  // drawn from whatever those five held.
  const resolvedInsights = useQuery(insightsQuery({ status: "RESOLVED" }));
  // Only for dating the rows resolved before `resolved_at` existed.
  const config = useQuery(effectiveConfigQuery());

  const s = stats.data;
  const evaluated = s?.traces_evaluated ?? 0;
  const top = insights.data?.insights ?? [];
  // The API returns runs oldest-first; a "recent" list reads the other way.
  const recentRuns = newestFirst(runs.data ?? []).slice(0, RECENT_RUNS);

  const autoResolveDays = autoResolveWindow(config.data?.config);
  const resolved = resolvedInsights.data?.insights ?? [];
  const trend = buildTrend({
    days: daily.data ?? [],
    resolved,
    autoResolveDays,
    span: DASHBOARD_DAYS,
  });
  const trendNote = resolvedNote(resolved, autoResolveDays);

  return (
    <div className="flex flex-col gap-10">
      <PageHeader
        title="Home"
        description="What AQuA has found in the observed agent."
      />

      <div data-tour="health">
        <StatBar />
      </div>
      <AskBar />

      {/* Two columns only from lg: at md each would be about 270px, and the
          charts' fourteen day links would be under the 24px WCAG 2.5.8 asks
          of a target. Those figures assume the rail at its default width; it
          can be dragged to 30%, and breakpoints follow the viewport, not this
          panel. A container query would follow the panel, but the Tailwind
          plugin for it is not installed, so a narrow panel falls back on the
          charts' own floor, which scrolls the row instead of shrinking the
          days.

          Side by side, Pipeline sits beside the latest insights and the two
          per-day charts share the row below, so their day columns line up. */}
      <div className="grid w-full min-w-0 grid-cols-1 gap-10 overflow-hidden lg:grid-cols-2">
        {/* The funnel subsumes the pass/fail/error split that used to sit
            here on its own: those three are its Evaluated bar. Two
            renderings of one set of figures, on one page, is two things that
            can disagree. */}
        <Section
          title="Pipeline"
          subtitle={
            stats.data
              ? `${stats.data.investigations.toLocaleString()} investigation${
                  stats.data.investigations === 1 ? "" : "s"
                }, ${HORIZON_LABEL}`
              : `What the investigations did with the trajectories, ${HORIZON_LABEL}`
          }
          action={
            <SectionLink to="/investigations">View investigations</SectionLink>
          }
          tour="pipeline"
        >
          <Pane
            query={stats}
            what="the pipeline figures"
            isEmpty={evaluated === 0}
            empty="No trajectories evaluated yet."
          >
            {s && <Funnel stats={s} linkStages />}
          </Pane>
        </Section>

        {/* First while the columns are stacked, so the worst issues stay on
            the first screen rather than below three charts. */}
        <Section
          title={view.title}
          subtitle={view.subtitle}
          tour="insights-strip"
          className="order-first lg:order-none"
          action={
            <div className="ml-auto flex shrink-0 items-center gap-2">
              <FilterTabs
                label="Which insights"
                options={STRIP_VIEW_NAMES}
                value={stripView}
                onSelect={setStripView}
                labelOf={formatSentenceCase}
              />
              <SectionLink
                to="/insights"
                search={
                  stripView === "recurring"
                    ? { status: "recurring" }
                    : undefined
                }
              >
                View all
              </SectionLink>
            </div>
          }
        >
          <Pane
            query={insights}
            what={`the ${view.title.toLowerCase()}`}
            isEmpty={top.length === 0}
            empty={
              stripView === "recurring"
                ? "Nothing has recurred yet."
                : "Nothing found yet."
            }
          >
            <ul className="flex flex-col gap-2">
              {top.map((i) => (
                <li key={i.insight_id}>
                  <Link
                    to="/insights/$insightId"
                    params={{ insightId: i.insight_id }}
                    className="block rounded-lg border p-3 hover:bg-muted/30"
                  >
                    <div className="flex items-start justify-between gap-3">
                      <p className={textStyle.itemTitle}>{i.label}</p>
                      <span className={cn(textStyle.meta, "shrink-0")}>
                        {i.trace_count}{" "}
                        {i.trace_count === 1 ? "trajectory" : "trajectories"}
                      </span>
                    </div>
                    {i.diagnosis && (
                      <p className={cn(textStyle.meta, "mt-1.5 line-clamp-2")}>
                        {i.diagnosis}
                      </p>
                    )}
                  </Link>
                </li>
              ))}
            </ul>
          </Pane>
        </Section>

        {/* The same chart the investigations page draws, from the same
            component: two renderings of one set of figures, on two pages, is
            two things that can disagree. */}
        <Section
          title="Activity"
          subtitle="Trajectories evaluated per day"
          help={CHART_HELP.activity}
          action={
            <SectionLink to="/investigations">View investigations</SectionLink>
          }
        >
          <Pane
            query={daily}
            what="the activity chart"
            isEmpty={(daily.data ?? []).length === 0}
            empty="No investigations have run."
          >
            <TracesPerDay
              days={daily.data ?? []}
              span={DASHBOARD_DAYS}
              height="h-20"
              linkDays
            />
          </Pane>
        </Section>

        {/* The Activity chart counts traces; this one counts issues.
            Whether the agent is getting better or worse is the question AQuA
            exists to answer, and until now nothing on the dashboard drew
            it. */}
        <Section
          title="Insights"
          subtitle="New and resolved per day"
          help={CHART_HELP.insightTrend(autoResolveDays)}
          action={<SectionLink to="/insights">View insights</SectionLink>}
        >
          <Pane
            query={daily}
            what="the insight trend"
            isEmpty={(daily.data ?? []).length === 0}
            empty="No investigations have run."
          >
            <InsightTrend days={trend} note={trendNote} height="h-20" />
          </Pane>
        </Section>
      </div>

      {/* "most recent", not "on record": this list is the newest few of a
          capped page, over no particular span, while the Pipeline figures
          above it are the horizon's. Two numbers on one page that count
          different things have to say which. The count is of the rows shown,
          not of the page they were cut from. */}
      <Section
        title="Recent investigations"
        subtitle={
          recentRuns.length > 0 ? `${recentRuns.length} most recent` : "\u00a0"
        }
        tour="recent-runs"
        action={
          <div className="flex shrink-0 items-center gap-4">
            <RunInvestigationButton />
            <SectionLink to="/investigations">View all</SectionLink>
          </div>
        }
      >
        <Pane
          query={runs}
          what="the recent investigations"
          isEmpty={(runs.data?.length ?? 0) === 0}
          empty="No investigations have run."
        >
          <RunsTable runs={recentRuns} history={runs.data} />
        </Pane>
      </Section>
    </div>
  );
}
