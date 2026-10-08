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

import { useCallback, useEffect, useRef, useState } from "react";
import {
  keepPreviousData,
  useMutation,
  useQuery,
  useQueryClient,
} from "@tanstack/react-query";
import { Link, useNavigate } from "@tanstack/react-router";
import { AlertCircle, Merge, Stethoscope, Trash2 } from "lucide-react";
import {
  dismissInsight,
  INSIGHTS_PAGE_SIZE,
  insightQuery,
  insightsQuery,
  isDiagnosed,
  noneDiagnosed,
  mergeInsights,
  type InsightView,
} from "@/lib/aqua-api";
import { MergeBar } from "./merge-bar";
import { DiagnoseInChatButton } from "./diagnose-in-chat-button";
import { Button } from "@/components/ui/button";
import { FailureNote } from "@/components/ui/failure-note";
import { RefreshButton } from "@/components/ui/refresh-button";
import { DayFilterChip } from "@/components/ui/day-filter-chip";
import { FilterTabs } from "@/components/ui/filter-tabs";
import { cn, formatSentenceCase } from "@/lib/utils";
import { link, mono, textStyle } from "@/lib/typography";
import { PageHeader } from "@/components/nav/page-header";
import { LoadingPane } from "@/components/ui/loading";

/** The statuses the list can be narrowed to, spelled as the URL spells them.
 *  No status is the whole list. */
export const INSIGHT_STATUS_FILTERS = ["new", "recurring", "resolved"] as const;
export type InsightStatusFilter = (typeof INSIGHT_STATUS_FILTERS)[number];
const STATUS_TABS = [undefined, ...INSIGHT_STATUS_FILTERS] as const;

/**
 * The insights list.
 *
 * `runId` and `embedded` are two questions, and were one prop until they were
 * separated here: `runId` narrows the read to one sweep's findings, `embedded`
 * says this list is inside another page and should bring no chrome of its own
 * — no heading, no scroll container, no page padding, no list-wide actions.
 *
 * They correlated perfectly while the run detail was the only embedder, which
 * is what hid the overload. They come apart the moment the standalone page
 * wants `?runId=`: that is a filtered list that still owns its page.
 *
 * The status filter is the caller's when it passes `onStatusChange`: `status`
 * is then the only record of it, and a tab click asks the caller to change it.
 * Without one the list keeps the filter itself, which is what an embedder
 * wants: the run detail narrows its list without the tab taking over its URL.
 *
 * `day` narrows the list to the insights found or resolved on that UTC day,
 * the status tab choosing which, and `onDayClear` is how the chip naming it
 * takes it off. The engine answers it, so the count and the pager describe the
 * day rather than the page they arrived on.
 */
export function InsightsView({
  runId,
  embedded = false,
  status,
  onStatusChange,
  day,
  onDayClear,
}: {
  runId?: string;
  embedded?: boolean;
  status?: InsightStatusFilter;
  onStatusChange?: (status: InsightStatusFilter | undefined) => void;
  day?: string;
  onDayClear?: () => void;
} = {}) {
  const [localStatus, setLocalStatus] = useState<InsightStatusFilter>();
  const statusFilter = onStatusChange ? status : localStatus;
  const setStatusFilter = onStatusChange ?? setLocalStatus;
  // The token for each page after the first, pushed as the reader walks
  // forward. Tokens are opaque, so the only way back to page N is the token
  // that reached it; keeping the trail is what makes Previous a read the
  // engine can serve rather than a slice of something already fetched.
  const [pageTokens, setPageTokens] = useState<string[]>([]);
  // A token addresses a position in the list it was issued for, so a new
  // filter starts at its own first page. Reset while rendering rather than in
  // an effect: an effect lets one render commit with the new filter and the
  // old token, and that page of the new list is fetched only to be discarded.
  const [pagedList, setPagedList] = useState({ runId, statusFilter, day });
  if (
    runId !== pagedList.runId ||
    statusFilter !== pagedList.statusFilter ||
    day !== pagedList.day
  ) {
    setPagedList({ runId, statusFilter, day });
    setPageTokens([]);
  }
  const tabsRef = useRef<HTMLDivElement>(null);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [picking, setPicking] = useState(false);
  const [activeIndex, setActiveIndex] = useState(0);

  const queryClient = useQueryClient();
  const navigate = useNavigate();

  const queryArgs = {
    runId,
    status: statusFilter?.toUpperCase(),
    pageSize: INSIGHTS_PAGE_SIZE,
    orderBy: "impact" as const,
    pageToken: pageTokens[pageTokens.length - 1],
    day,
  };
  const { data, isLoading, isError, error, refetch, isFetching } = useQuery({
    ...insightsQuery(queryArgs),
    // Each page is its own query key, so without this the list unmounts into
    // the loading pane on every Next and the reader loses their place.
    placeholderData: keepPreviousData,
  });

  const dismiss = useMutation({
    mutationFn: dismissInsight,
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["aqua", "insights"] });
    },
  });

  const merge = useMutation({
    mutationFn: async ({
      sourceIds,
      targetInsightId,
    }: {
      sourceIds: string[];
      targetInsightId: string;
    }) => {
      await Promise.all(
        sourceIds.map((insightId) =>
          mergeInsights({ insightId, targetInsightId }),
        ),
      );
    },
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["aqua", "insights"] });
      setSelected(new Set());
      setPicking(false);
    },
  });

  const toggleSelect = useCallback((insightId: string) => {
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(insightId)) next.delete(insightId);
      else next.add(insightId);
      return next;
    });
  }, []);

  const startPicking = useCallback(() => {
    setPicking(true);
    setSelected(new Set());
  }, []);

  const cancelPicking = useCallback(() => {
    setPicking(false);
    setSelected(new Set());
  }, []);

  // Drawn exactly as it arrived: this page is already the engine's answer to
  // "the ten highest-impact issues matching this filter".
  const insights = data?.insights ?? [];
  const page = pageTokens.length + 1;
  // `total` counts every match, so it outlives the page. An engine that omits
  // it answers 0, and the rows in hand are a better floor than that.
  const matching = Math.max(data?.total ?? 0, insights.length);
  const totalPages = Math.max(1, Math.ceil(matching / INSIGHTS_PAGE_SIZE));
  const nextPageToken = data?.next_page_token ?? null;
  // The server's count over every matching insight, not this page. Undefined
  // from an agent deployed before the field, and then nothing is claimed.
  const conversations = data?.conversations;

  useEffect(() => {
    if (activeIndex >= insights.length) {
      setActiveIndex(Math.max(0, insights.length - 1));
    }
  }, [insights.length, activeIndex]);

  const onKeyDown = useCallback(
    (e: React.KeyboardEvent) => {
      if (e.target !== e.currentTarget) return;
      if (e.key === "j" || e.key === "ArrowDown") {
        e.preventDefault();
        setActiveIndex((i) => Math.min(insights.length - 1, i + 1));
      } else if (e.key === "k" || e.key === "ArrowUp") {
        e.preventDefault();
        setActiveIndex((i) => Math.max(0, i - 1));
      } else if (e.key === "x" && picking) {
        e.preventDefault();
        const target = insights[activeIndex];
        if (target) toggleSelect(target.insight_id);
      } else if (e.key === "Enter" && !picking) {
        const target = insights[activeIndex];
        if (target) {
          void navigate({
            to: "/insights/$insightId",
            params: { insightId: target.insight_id },
          });
        }
      }
    },
    [insights, activeIndex, navigate, picking, toggleSelect],
  );

  if (isLoading) {
    return <LoadingPane label="Loading insights…" />;
  }
  if (isError) {
    // Embedded, this sits under a page that has already explained the state --
    // the run detail's own read fails for the same reason -- and inside a card
    // that `Centered` would try to fill.
    const note = (
      <FailureNote
        lead="Couldn't load insights"
        error={error}
        onRetry={() => void refetch()}
        compact={embedded}
        className={cn(embedded && "py-2")}
      />
    );
    return embedded ? note : <Centered>{note}</Centered>;
  }

  const diagnosed = insights.filter(isDiagnosed).length;

  // An embedded list with nothing in it is a line of text inside someone
  // else's page. Not once it has been paged, though: then the empty page needs
  // its pager, because Previous is the way off it. Nor once a status narrows
  // it: the tabs are the way back to the rest.
  if (
    embedded &&
    insights.length === 0 &&
    pageTokens.length === 0 &&
    !statusFilter
  ) {
    return (
      <p className={cn(textStyle.meta, "py-2")}>
        {emptyText(runId, day, statusFilter)}
      </p>
    );
  }

  // The chip unmounts with the day, so focus goes to the tabs it sat beside.
  const clearDay = () => {
    onDayClear?.();
    tabsRef.current
      ?.querySelector<HTMLButtonElement>('[aria-pressed="true"]')
      ?.focus();
  };

  const counts =
    insights.length > 0 ? (
      <>
        {`${matching.toLocaleString()} ${
          matching === 1 ? "insight" : "insights"
        }${day ? ` ${dayChange(statusFilter)} ${day} (UTC)` : ""} · `}
        {diagnosed} diagnosed, {insights.length - diagnosed} undiagnosed
        {totalPages > 1 && " on this page"}
        {conversations !== undefined &&
          ` · ${conversations.toLocaleString()} ${
            conversations === 1 ? "trajectory" : "trajectories"
          } affected`}
      </>
    ) : undefined;

  const controls = (
    <div className="flex flex-wrap items-center justify-between gap-2 pt-1 border-t border-border/40">
      <div className="flex flex-wrap items-center gap-2">
        <FilterTabs
          ref={tabsRef}
          label="Status"
          // A day's column counts new and resolved; nothing recurs on a day.
          options={STATUS_TABS.filter((s) => !(day && s === "recurring"))}
          value={statusFilter}
          // The selected tab leaves the status alone, so the reset above
          // never fires; clicking it still means "from the top".
          onSelect={(s) =>
            s === statusFilter ? setPageTokens([]) : setStatusFilter(s)
          }
          labelOf={(s) => formatSentenceCase(s ?? "all")}
        />
        {day && onDayClear && <DayFilterChip day={day} onClear={clearDay} />}
      </div>

      {!embedded && (
        <div className="flex items-center gap-2">
          <RefreshButton
            onRefresh={() => void refetch()}
            refreshing={isFetching}
          />
          {!picking ? (
            <Button
              size="sm"
              variant="outline"
              className="h-7 gap-1.5 font-normal"
              onClick={startPicking}
            >
              <Merge className="h-3 w-3" /> Merge duplicates…
            </Button>
          ) : null}
        </div>
      )}
    </div>
  );

  return (
    // biome-ignore lint/a11y/noStaticElementInteractions: the list takes focus as a keyboard scope for its j/k/x/Enter shortcuts.
    <div
      className="outline-none"
      // biome-ignore lint/a11y/noNoninteractiveTabindex: the list takes focus as a keyboard scope for its j/k/x/Enter shortcuts.
      tabIndex={0}
      onKeyDown={onKeyDown}
      data-testid="issues-list"
    >
      <div className="flex flex-col gap-4">
        {embedded ? (
          <header className="flex flex-col gap-2">
            {counts && <span className={textStyle.description}>{counts}</span>}
            {controls}
          </header>
        ) : (
          <PageHeader
            data-tour="insights-triage"
            className="gap-2"
            title={insights[0]?.agent_name || "Insights"}
            description={counts}
          >
            {runId && (
              <p className={textStyle.meta}>
                Showing only what investigation{" "}
                <span className={mono}>{runId.slice(0, 8)}</span> found.{" "}
                <Link to="/insights" search={{}} className={link.inline}>
                  Show all insights
                </Link>
              </p>
            )}
            {controls}
          </PageHeader>
        )}

        {noneDiagnosed(data?.insights ?? []) && (
          <p
            className={cn(
              textStyle.meta,
              "rounded-md border border-amber-500/40 bg-amber-500/10 p-2 text-amber-500",
            )}
          >
            Nothing here has a root cause recorded yet. Root-cause analysis runs
            when you ask AQuA to diagnose an insight, so until then these are
            what the judge model saw rather than why it happened.
            <span className="mt-2 block">
              Click <strong>Chat</strong> on an undiagnosed insight to run your
              first root-cause analysis.
            </span>
          </p>
        )}

        {picking && (
          <MergeBar
            insights={insights}
            selected={selected}
            onCancel={cancelPicking}
            onMerge={(targetInsightId) => {
              const sourceIds = [...selected].filter(
                (id) => id !== targetInsightId,
              );
              if (sourceIds.length > 0) {
                merge.mutate({ sourceIds, targetInsightId });
              }
            }}
            merging={merge.isPending}
          />
        )}

        {insights.length === 0 ? (
          <p className={cn(textStyle.description, "py-6")}>
            {emptyText(runId, day, statusFilter)}
          </p>
        ) : (
          <div className="flex flex-col gap-2.5">
            {insights.map((insight, index) => (
              <IssueCard
                key={insight.insight_id}
                insight={insight}
                active={index === activeIndex}
                picking={picking}
                selected={selected.has(insight.insight_id)}
                onToggleSelect={() => toggleSelect(insight.insight_id)}
                onDismiss={() => dismiss.mutate(insight.insight_id)}
                dismissing={
                  dismiss.isPending && dismiss.variables === insight.insight_id
                }
              />
            ))}
          </div>
        )}

        {(pageTokens.length > 0 || nextPageToken) && (
          <div
            className={cn(
              textStyle.meta,
              "mt-2 flex items-center justify-between border-t border-border/40 pt-3",
            )}
          >
            <Button
              size="sm"
              variant="outline"
              disabled={pageTokens.length === 0 || isFetching}
              onClick={() => setPageTokens((tokens) => tokens.slice(0, -1))}
              className="h-7"
            >
              Previous
            </Button>
            <span>
              Page {page} of {totalPages} ({insights.length} of{" "}
              {matching.toLocaleString()})
            </span>
            <Button
              size="sm"
              variant="outline"
              disabled={!nextPageToken || isFetching}
              onClick={() =>
                setPageTokens((tokens) =>
                  nextPageToken ? [...tokens, nextPageToken] : tokens,
                )
              }
              className="h-7"
            >
              Next
            </Button>
          </div>
        )}
      </div>
    </div>
  );
}

export function IssueCard({
  insight,
  active = false,
  picking = false,
  selected = false,
  onToggleSelect,
  onDismiss,
  dismissing = false,
}: {
  insight: InsightView;
  active?: boolean;
  picking?: boolean;
  selected?: boolean;
  onToggleSelect?: () => void;
  onDismiss?: () => void;
  dismissing?: boolean;
}) {
  const queryClient = useQueryClient();
  const headline = insight.diagnosis || insight.label;

  const cardContent = (
    <div className="flex items-start gap-2.5">
      {picking && (
        <input
          type="checkbox"
          checked={selected}
          onClick={(e) => e.stopPropagation()}
          onChange={() => onToggleSelect?.()}
          className="mt-1 h-3.5 w-3.5 rounded border-border text-primary focus:ring-1 focus:ring-primary"
          aria-label={`Select ${headline} for merging`}
        />
      )}

      {/* Negative counterpart of the root-cause chip below; the two never show
          together. An autorater diagnosis suppresses both -- the headline it
          writes already answers the question. */}
      {!isDiagnosed(insight) && (
        <span className={cn(textStyle.meta, "mt-0.5 shrink-0")}>
          Undiagnosed
        </span>
      )}

      <div className="min-w-0 flex-1">
        <div className="flex items-start justify-between gap-2">
          <p className={textStyle.itemTitle}>{headline}</p>
          <div className="flex shrink-0 items-center gap-0.5">
            {insight.has_root_cause && (
              <span
                data-testid="root-cause-chip"
                title="An RCA turn recorded a root cause for this insight"
                className={cn(
                  textStyle.meta,
                  "mr-1 flex items-center gap-1 rounded-full border border-border/40 bg-muted/40 px-2 py-0.5",
                )}
              >
                <Stethoscope className="h-3 w-3" />
                root cause
              </span>
            )}
            {/* Hidden during merge selection so clicks only toggle card selection. */}
            {!picking && (
              <DiagnoseInChatButton
                insightId={insight.insight_id}
                label={insight.label}
                ariaLabel={`Diagnose insight in chat: ${headline}`}
                className="h-6 px-1.5 text-muted-foreground hover:text-foreground"
              />
            )}
            {onDismiss && (
              <Button
                size="sm"
                variant="ghost"
                disabled={dismissing}
                onClick={(e) => {
                  e.preventDefault();
                  e.stopPropagation();
                  onDismiss();
                }}
                className="h-6 px-1.5 text-muted-foreground hover:text-destructive"
                aria-label={`Dismiss insight: ${headline}`}
              >
                <Trash2 className="h-3 w-3 mr-1" />
                {dismissing ? "Dismissing…" : "Dismiss"}
              </Button>
            )}
          </div>
        </div>

        {!isDiagnosed(insight) && (
          <p className={cn(textStyle.meta, "mt-1 flex items-center gap-1")}>
            <AlertCircle className="h-3 w-3" />
            No root cause recorded yet: this is what the judge model saw, not
            why it happened.
          </p>
        )}
        <p className={cn(textStyle.meta, "mt-2 flex flex-wrap gap-3")}>
          <span>{formatSentenceCase(insight.status.toLowerCase())}</span>
          <span>
            {insight.occurrence_count}{" "}
            {insight.occurrence_count === 1 ? "occurrence" : "occurrences"}
          </span>
          <span>
            {insight.trace_count}{" "}
            {insight.trace_count === 1 ? "trajectory" : "trajectories"}
          </span>
          <span className="ml-auto">{insight.impact} impact</span>
        </p>
      </div>
    </div>
  );

  if (picking) {
    return (
      // biome-ignore lint/a11y/noStaticElementInteractions: the checkbox inside the card is the keyboard path; the card click only enlarges the pointer target.
      // biome-ignore lint/a11y/useKeyWithClickEvents: the checkbox inside the card is the keyboard path; the card click only enlarges the pointer target.
      <div
        onClick={onToggleSelect}
        className={cn(
          "cursor-pointer rounded-lg border p-3 transition-colors",
          selected
            ? "border-primary bg-primary/5"
            : "border-border hover:border-foreground/30",
          active && "ring-1 ring-primary/60",
        )}
      >
        {cardContent}
      </div>
    );
  }

  return (
    <Link
      to="/insights/$insightId"
      params={{ insightId: insight.insight_id }}
      onMouseEnter={() =>
        queryClient.prefetchQuery(insightQuery(insight.insight_id))
      }
      className={cn(
        "block rounded-lg border p-3 transition-colors hover:border-foreground/30",
        isDiagnosed(insight) ? "border-emerald-500/40" : "border-border",
        active && "ring-1 ring-primary/60",
      )}
    >
      {cardContent}
    </Link>
  );
}

/** Why the list is empty, which depends on what was asked for rather than on
 *  where the list is drawn — so the run detail and a `?runId=` page give the
 *  same answer. */
function emptyText(
  runId?: string,
  day?: string,
  status?: InsightStatusFilter,
): string {
  if (runId) {
    const run = runId.slice(0, 8);
    return status
      ? `None of investigation ${run}'s insights are ${status}.`
      : `Investigation ${run} produced no insights.`;
  }
  if (day) return `No insights were ${dayChange(status)} ${day} (UTC).`;
  return "No insights found for this filter.";
}

/** The change a day's list holds under each tab: the chart's two series. */
function dayChange(status?: InsightStatusFilter): string {
  if (status === "new") return "found on";
  if (status === "resolved") return "resolved on";
  return "found or resolved on";
}

function Centered({ children }: { children: React.ReactNode }) {
  return (
    <div
      className={cn(
        textStyle.description,
        "flex h-full flex-1 items-center justify-center p-6",
      )}
    >
      {children}
    </div>
  );
}
