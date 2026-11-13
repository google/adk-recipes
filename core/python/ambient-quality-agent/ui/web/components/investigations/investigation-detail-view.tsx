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
import { ArrowLeft } from "lucide-react";
import { investigationQuery } from "@/lib/aqua-api";
import { describeFailure } from "@/lib/failure";
import { FailureNote } from "@/components/ui/failure-note";
import { investigationEventsToTrajectoryDoc } from "@/lib/trajectory/investigationEventsToTrajectoryDoc";
import { CounterPanel } from "./counter-panel";
import { CustomMetricsPanel } from "./custom-metrics-panel";
import { InvestigationEventLedger } from "./investigation-event-ledger";
import { Trajectory } from "@/components/trajectory/trajectory";
import { ConversationLink } from "@/components/investigations/conversation-link";
import {
  InsightsView,
  type InsightStatusFilter,
} from "@/components/insights/insights-view";
import { Funnel } from "@/components/stats/funnel";
import { HelpPopover } from "@/components/ui/help-popover";
import {
  agentLabel,
  extractReviewFocus,
  formatInstant,
  isScheduled,
  noCountsText,
  buildInvestigationStats,
  renderScheduledText,
  telemetryWindow,
} from "@/lib/investigation-summary";
import { RawJson } from "@/components/ui/raw-json";
import { TriggerBadge } from "./trigger-badge";
import { PageHeader } from "@/components/nav/page-header";
import { mono, textStyle } from "@/lib/typography";
import { cn, formatSentenceCase } from "@/lib/utils";
import { useAgentKey } from "@/lib/agent-selection";

/**
 * One investigation: its summary, the insights it found, and its counters.
 *
 * The insights list's status tab and page are the caller's when it passes
 * their change handlers, as `InsightsView` describes; without them the list
 * keeps both itself.
 */
export function InvestigationDetailView({
  investigationId,
  insightStatus,
  onInsightStatusChange,
  insightPage,
  onInsightPageChange,
}: {
  investigationId: string;
  insightStatus?: InsightStatusFilter;
  onInsightStatusChange?: (status: InsightStatusFilter | undefined) => void;
  insightPage?: number;
  onInsightPageChange?: (page: number | undefined) => void;
}) {
  const agentKey = useAgentKey();
  const { data, isLoading, isError, error } = useQuery(
    investigationQuery(agentKey, investigationId),
  );

  if (isLoading) {
    return (
      <div className={textStyle.description}>
        Loading investigation {investigationId.slice(0, 8)}…
      </div>
    );
  }

  if (isError) {
    return (
      <FailureNote lead="Couldn't load this investigation" error={error} />
    );
  }

  if (!data?.investigation) {
    return (
      <div className={textStyle.description}>
        No record returned for {investigationId}.
      </div>
    );
  }

  const investigation = data.investigation;
  const passed = investigation.metrics_passed ?? 0;
  const failed = investigation.metrics_failed ?? 0;
  const errored = investigation.metrics_errored ?? 0;
  const totalScored = passed + failed + errored;
  // A sweep that found nothing to read is reported as a failure by the record
  // and as an empty result by this page: the investigation did stop early, but
  // "your agent has not been used yet" is the finding, not a defect in AQuA.
  const failure = investigation.error
    ? describeFailure(investigation.error)
    : null;
  const stats = buildInvestigationStats(investigation);
  // Evaluate fields individually because either override can be set independently.
  const selectorSql = investigation.custom_overrides?.selector_sql?.trim();
  const reviewFocus = extractReviewFocus(investigation);

  return (
    <div className="flex flex-col gap-6">
      <Link
        to="/investigations"
        className={cn(
          textStyle.meta,
          "inline-flex w-fit items-center gap-1 hover:text-foreground",
        )}
      >
        <ArrowLeft className="h-3 w-3" /> All investigations
      </Link>

      <PageHeader
        data-tour="investigation-header"
        className="gap-2"
        title={
          <>
            Investigation{" "}
            <span className={mono}>{investigation.investigation_id}</span>
          </>
        }
        aside={
          <span className="flex items-center gap-2">
            {/* Who asked for this investigation. A manual investigation and a
                  scheduled one that found the same thing mean different things:
                  one is somebody looking, the other is the loop turning. */}
            <TriggerBadge investigation={investigation} className="py-0.5" />
            <span
              className={cn(
                textStyle.label,
                "rounded border px-2 py-0.5",
                failure?.kind === "error" &&
                  "border-destructive text-destructive bg-destructive/10",
                (failure?.kind === "no-data" ||
                  (!failure && isScheduled(investigation))) &&
                  "border-border text-muted-foreground",
                !failure &&
                  !isScheduled(investigation) &&
                  "border-emerald-500/40 text-emerald-500 bg-emerald-500/10",
              )}
            >
              {formatSentenceCase(
                investigation.status ||
                  (investigation.error ? "failed" : "done"),
              )}
            </span>
          </span>
        }
      >
        <div
          className={cn(textStyle.meta, "flex flex-wrap items-center gap-4")}
        >
          <span>
            Observed agent:{" "}
            <strong className="text-foreground">
              {agentLabel(investigation)}
            </strong>
          </span>
          {/* The period examined, which the elapsed time below says nothing
                about -- a 40s investigation can cover a week. */}
          <span>Telemetry window: {telemetryWindow(investigation)}</span>
          {/* A scheduled investigation has not started: its record was written
                when the agent redeploy arrived. */}
          <span>
            {isScheduled(investigation) ? "Recorded" : "Started"}:{" "}
            {new Date(investigation.created_at).toLocaleString()}
          </span>
          {/* An investigation an agent redeploy set waits for its trigger, and
                the window above is unset until it starts. */}
          {investigation.due_at && (
            <span title={renderScheduledText(investigation) ?? undefined}>
              Due: {formatInstant(investigation.due_at)}
            </span>
          )}
          <span>Finished: {formatInstant(investigation.finished_at)}</span>
          {investigation.elapsed_seconds !== null &&
            investigation.elapsed_seconds !== undefined && (
              <span>Duration: {investigation.elapsed_seconds.toFixed(1)}s</span>
            )}
          {/* `0` is the default and means unbounded, so it is shown rather
                than folded into "not set" -- the two answer the question
                "why did it stop at N trajectories?" differently. */}
          {investigation.budget_per_metric !== null &&
            investigation.budget_per_metric !== undefined && (
              <span>Budget per metric: {investigation.budget_per_metric}</span>
            )}
          {/* Named for its unit: the pipeline below counts trajectories,
                and these run several times higher. */}
          {totalScored > 0 && (
            <span className="inline-flex flex-wrap items-center gap-1">
              Metric outcomes
              <HelpPopover topic="metric outcomes">
                One outcome per trajectory and metric, so several for each
                trajectory. The pipeline below counts trajectories.
              </HelpPopover>
              <strong className="text-emerald-500">{passed} passed</strong> /{" "}
              <strong
                className={failed > 0 ? "text-orange-400" : "text-foreground"}
              >
                {failed} failed
              </strong>
              {errored > 0 && (
                <>
                  /{" "}
                  <strong className="text-destructive">
                    {errored}{" "}
                    {errored === 1
                      ? "eval service error"
                      : "eval service errors"}
                  </strong>
                </>
              )}
            </span>
          )}
          <ConversationLink
            contextId={investigation.context_id}
            triggerType={investigation.trigger_type}
          />
        </div>

        {/* Rendered as plain text to prevent markup injection from LLM-generated overrides. */}
        {(selectorSql || reviewFocus) && (
          <div className={cn(textStyle.meta, "flex flex-col gap-2")}>
            {selectorSql && (
              <div className="flex flex-col gap-1">
                <span>Selector SQL</span>
                <pre
                  className={cn(
                    textStyle.code,
                    "max-h-64 overflow-auto rounded-md border bg-muted/40 p-2 whitespace-pre-wrap break-words",
                  )}
                >
                  {selectorSql}
                </pre>
              </div>
            )}
            {reviewFocus && (
              <span>
                Review focus:{" "}
                <span className="text-foreground">{reviewFocus}</span>
              </span>
            )}
          </div>
        )}
      </PageHeader>

      {failure && (
        <div
          className={cn(
            "rounded-lg border p-3",
            failure.kind === "error"
              ? "border-destructive/40 bg-destructive/10"
              : "border-dashed",
          )}
        >
          <FailureNote
            lead="This investigation stopped early"
            error={investigation.error}
          />
        </div>
      )}

      {/* This investigation's funnel, drawn by the component that draws the
            totals on Home and the investigations page, so the three read
            alike. */}
      <section className="flex flex-col gap-2 rounded-lg border bg-card p-4">
        <h2 className={textStyle.sectionTitle}>Pipeline</h2>
        {stats ? (
          <Funnel stats={stats} />
        ) : (
          <p className={textStyle.meta}>{noCountsText(investigation)}</p>
        )}
      </section>

      {/* The insights this investigation was the sighting for. The insight
            store is the record of what a sweep found, so it is read directly
            rather than through a per-investigation report: one writer, and a
            finding stays reachable from the investigation that raised it and
            from the issue it belongs to. */}
      <section className="flex flex-col gap-2 rounded-lg border bg-card p-4">
        <h2 className={textStyle.sectionTitle}>
          Insights from this investigation
        </h2>
        <InsightsView
          investigationId={investigation.investigation_id}
          embedded
          status={insightStatus}
          onStatusChange={onInsightStatusChange}
          page={insightPage}
          onPageChange={onInsightPageChange}
        />
      </section>

      {/* Summary Counters */}
      <section className="flex flex-col gap-2 rounded-lg border bg-card p-4">
        <h2 className={textStyle.sectionTitle}>Investigation counters</h2>
        <p className={textStyle.description}>
          Stage-by-stage counts across the entire investigation.
        </p>
        <CounterPanel counters={investigation.counters} />
      </section>

      {/* Gate on metrics_detail or metrics_by_name so investigations with
            either published detail or outcomes render. */}
      {(Object.keys(investigation.summary?.metrics_detail ?? {}).length > 0 ||
        Object.keys(investigation.summary?.metrics_by_name ?? {}).length >
          0) && (
        <section className="flex flex-col gap-2 rounded-lg border bg-card p-4">
          <h2 className={textStyle.sectionTitle}>Custom metrics</h2>
          <p className={textStyle.description}>
            Execution mode, pass threshold, and trajectory outcomes for each
            published custom metric.
          </p>
          <CustomMetricsPanel
            outcomes={investigation.summary?.metrics_by_name}
            detail={investigation.summary?.metrics_detail}
          />
        </section>
      )}

      {/* Investigation event narrative */}
      {investigation.events && investigation.events.length > 0 && (
        <>
          <section className="flex flex-col gap-2 rounded-lg border bg-card p-4">
            <h2 className={textStyle.sectionTitle}>Workflow narrative</h2>
            <p className={textStyle.description}>
              Node events and executed analysis code in chronological order.
            </p>
            <InvestigationEventLedger events={investigation.events} />
          </section>

          <section className="flex flex-col gap-2 rounded-lg border bg-card p-4">
            <h2 className={textStyle.sectionTitle}>Step ledger</h2>
            <p className={textStyle.description}>
              Click any step to inspect tools, arguments, and raw data.
            </p>
            <Trajectory
              doc={investigationEventsToTrajectoryDoc(investigation.events)}
            />
          </section>
        </>
      )}

      <RawJson label="Raw investigation" value={investigation} />
    </div>
  );
}
