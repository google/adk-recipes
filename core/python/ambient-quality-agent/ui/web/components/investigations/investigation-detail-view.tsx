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
import { fromRunEvents } from "@/lib/trajectory/fromRunEvents";
import { CounterPanel } from "./counter-panel";
import { CustomMetricsPanel } from "./custom-metrics-panel";
import { RunEventLedger } from "./run-event-ledger";
import { Trajectory } from "@/components/trajectory/trajectory";
import { ConversationLink } from "@/components/investigations/conversation-link";
import { InsightsView } from "@/components/insights/insights-view";
import { Funnel } from "@/components/stats/funnel";
import { HelpPopover } from "@/components/ui/help-popover";
import {
  ABSENT,
  agentLabel,
  formatInstant,
  isScheduled,
  noCountsText,
  runStats,
  renderScheduledText,
  telemetryWindow,
} from "@/lib/run-summary";
import { RawJson } from "@/components/ui/raw-json";
import { PageHeader } from "@/components/nav/page-header";
import { mono, textStyle } from "@/lib/typography";
import { cn, formatSentenceCase } from "@/lib/utils";

export function InvestigationDetailView({ runId }: { runId: string }) {
  const { data, isLoading, isError, error } = useQuery(
    investigationQuery(runId),
  );

  if (isLoading) {
    return (
      <div className={textStyle.description}>
        Loading investigation {runId.slice(0, 8)}…
      </div>
    );
  }

  if (isError) {
    return (
      <FailureNote lead="Couldn't load this investigation" error={error} />
    );
  }

  if (!data?.run) {
    return (
      <div className={textStyle.description}>
        No record returned for {runId}.
      </div>
    );
  }

  const run = data.run;
  const passed = run.metrics_passed ?? 0;
  const failed = run.metrics_failed ?? 0;
  const errored = run.metrics_errored ?? 0;
  const totalScored = passed + failed + errored;
  // A sweep that found nothing to read is reported as a failure by the record
  // and as an empty result by this page: the run did stop early, but "your
  // agent has not been used yet" is the finding, not a defect in AQuA.
  const failure = run.error ? describeFailure(run.error) : null;
  const stats = runStats(run);
  // Evaluate fields individually because either override can be set independently.
  const selectorSql = run.custom_overrides?.selector_sql?.trim();
  const reviewFocus = run.custom_overrides?.session_review_focus?.trim();

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
        data-tour="run-header"
        className="gap-2"
        title={
          <>
            Investigation <span className={mono}>{run.run_id}</span>
          </>
        }
        aside={
          <span
            className={cn(
              textStyle.label,
              "rounded border px-2 py-0.5",
              failure?.kind === "error" &&
                "border-destructive text-destructive bg-destructive/10",
              (failure?.kind === "no-data" || (!failure && isScheduled(run))) &&
                "border-border text-muted-foreground",
              !failure &&
                !isScheduled(run) &&
                "border-emerald-500/40 text-emerald-500 bg-emerald-500/10",
            )}
          >
            {formatSentenceCase(run.status || (run.error ? "failed" : "done"))}
          </span>
        }
      >
        <div
          className={cn(textStyle.meta, "flex flex-wrap items-center gap-4")}
        >
          <span>
            Observed agent:{" "}
            <strong className="text-foreground">{agentLabel(run)}</strong>
          </span>
          {/* Who asked for this sweep. A `manual` run and a `scheduled` one
                that found the same thing mean different things: one is
                somebody looking, the other is the loop turning. */}
          <span>Trigger: {run.trigger_type || ABSENT}</span>
          {/* The period examined, which the elapsed time below says nothing
                about -- a 40s run can cover a week. */}
          <span>Telemetry window: {telemetryWindow(run)}</span>
          {/* A scheduled run has not started: its record was written when
                the agent redeploy arrived. */}
          <span>
            {isScheduled(run) ? "Recorded" : "Started"}:{" "}
            {new Date(run.created_at).toLocaleString()}
          </span>
          {/* A run an agent redeploy set waits for its trigger, and the
                window above is unset until it starts. */}
          {run.due_at && (
            <span title={renderScheduledText(run) ?? undefined}>
              Due: {formatInstant(run.due_at)}
            </span>
          )}
          <span>Finished: {formatInstant(run.finished_at)}</span>
          {run.elapsed_seconds !== null &&
            run.elapsed_seconds !== undefined && (
              <span>Duration: {run.elapsed_seconds.toFixed(1)}s</span>
            )}
          {/* `0` is the default and means unbounded, so it is shown rather
                than folded into "not set" -- the two answer the question
                "why did it stop at N traces?" differently. */}
          {run.budget_per_metric !== null &&
            run.budget_per_metric !== undefined && (
              <span>Budget per metric: {run.budget_per_metric}</span>
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
            contextId={run.context_id}
            triggerType={run.trigger_type}
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
            error={run.error}
          />
        </div>
      )}

      {/* This run's funnel, drawn by the component that draws the totals on
            Home and the investigations page, so the three read alike. */}
      <section className="flex flex-col gap-2 rounded-lg border bg-card p-4">
        <h2 className={textStyle.sectionTitle}>Pipeline</h2>
        {stats ? (
          <Funnel stats={stats} />
        ) : (
          <p className={textStyle.meta}>{noCountsText(run)}</p>
        )}
      </section>

      {/* The insights this run was the sighting for. The insight store is the
            record of what a sweep found, so it is read directly rather than
            through a per-run report: one writer, and a finding stays reachable
            from the run that raised it and from the issue it belongs to. */}
      <section className="flex flex-col gap-2 rounded-lg border bg-card p-4">
        <h2 className={textStyle.sectionTitle}>
          Insights from this investigation
        </h2>
        <InsightsView runId={run.run_id} embedded />
      </section>

      {/* Summary Counters */}
      <section className="flex flex-col gap-2 rounded-lg border bg-card p-4">
        <h2 className={textStyle.sectionTitle}>Investigation counters</h2>
        <p className={textStyle.description}>
          Stage-by-stage counts across the entire investigation.
        </p>
        <CounterPanel counters={run.counters} />
      </section>

      {/* Gate on metrics_detail or metrics_by_name so runs with either
            published detail or outcomes render. */}
      {(Object.keys(run.summary?.metrics_detail ?? {}).length > 0 ||
        Object.keys(run.summary?.metrics_by_name ?? {}).length > 0) && (
        <section className="flex flex-col gap-2 rounded-lg border bg-card p-4">
          <h2 className={textStyle.sectionTitle}>Custom metrics</h2>
          <p className={textStyle.description}>
            Execution mode, pass threshold, and trajectory outcomes for each
            published custom metric.
          </p>
          <CustomMetricsPanel
            outcomes={run.summary?.metrics_by_name}
            detail={run.summary?.metrics_detail}
          />
        </section>
      )}

      {/* Run Event Narrative */}
      {run.events && run.events.length > 0 && (
        <>
          <section className="flex flex-col gap-2 rounded-lg border bg-card p-4">
            <h2 className={textStyle.sectionTitle}>Workflow narrative</h2>
            <p className={textStyle.description}>
              Node events and executed analysis code in chronological order.
            </p>
            <RunEventLedger events={run.events} />
          </section>

          <section className="flex flex-col gap-2 rounded-lg border bg-card p-4">
            <h2 className={textStyle.sectionTitle}>Step ledger</h2>
            <p className={textStyle.description}>
              Click any step to inspect tools, arguments, and raw data.
            </p>
            <Trajectory doc={fromRunEvents(run.events)} />
          </section>
        </>
      )}

      <RawJson label="Raw investigation" value={run} />
    </div>
  );
}
