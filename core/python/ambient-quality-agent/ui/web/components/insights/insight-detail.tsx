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

import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Link } from "@tanstack/react-router";
import {
  ArrowLeft,
  Check,
  Copy,
  ExternalLink,
  MessageSquare,
  Search,
} from "lucide-react";

/** Findings recorded straight from a conversation carry `chat-<contextId>`
 *  rather than a run id; they have no investigation page and no sweep
 *  artifact behind them. Mirrors `CHAT_RUN_PREFIX` in investigation_tools.py. */
const CHAT_RUN_PREFIX = "chat-";
/** Mirrors `investigation_tools.UNATTRIBUTED`: recorded in a chat we cannot
 *  name, so there is nothing to link to. */
const UNATTRIBUTED = "unattributed";
const isChatRun = (runId: string) => runId.startsWith(CHAT_RUN_PREFIX);
const chatContextId = (runId: string): string | null => {
  const id = runId.slice(CHAT_RUN_PREFIX.length);
  return id && id !== UNATTRIBUTED ? id : null;
};
import {
  insightQuery,
  isDiagnosed,
  isOccurrenceDiagnosed,
  type InsightOccurrence,
  type InsightView,
  type RootCause,
} from "@/lib/aqua-api";
import { Button } from "@/components/ui/button";
import { DiagnoseInChatButton } from "./diagnose-in-chat-button";
import { RootCauseRecord, newestRootCause } from "./root-cause-record";
import { cn, formatSentenceCase } from "@/lib/utils";
import { link, mono, textStyle } from "@/lib/typography";
import { PageHeader } from "@/components/nav/page-header";
import { FailureNote } from "@/components/ui/failure-note";
import { LoadingPane } from "@/components/ui/loading";
import { RawJson } from "@/components/ui/raw-json";

/** One sampled finding: the two behaviours, kept apart. */
export interface RubricFinding {
  expected: string;
  actual: string;
  /** Used only when neither behaviour is readable, so the row still points at
   *  something rather than printing the object. */
  fallback: string;
}

/** Read one sampled finding off a sighting's rubric.
 *
 * `review` asks the model for both behaviours and drops a finding carrying
 * neither, so one of the two can be absent and both usually are not.
 *
 * Shapes are read defensively rather than typed: this payload crosses from a
 * sweep's own record straight into the pane, and a rubric that does not match
 * is worth showing as its id rather than as a stack trace.
 */
export function rubricFinding(raw: Record<string, unknown>): RubricFinding {
  const rubric = (raw.rubric ?? {}) as Record<string, unknown>;
  const text = (k: string, from: Record<string, unknown>): string =>
    typeof from[k] === "string" ? (from[k] as string).trim() : "";

  const actual =
    text("actual_behavior", rubric) || text("actual_behavior", raw);
  const expected =
    text("expected_behavior", rubric) || text("expected_behavior", raw);

  const id =
    text("eval_case_id", raw) ||
    text("session_id", rubric) ||
    text("rubric_id", raw) ||
    text("name", raw);

  return {
    expected,
    actual,
    fallback: id ? `Finding ${id}` : "Finding (no detail recorded)",
  };
}

/** What the agent should have done, against what it did.
 *
 * Expected first, as an assertion failure prints it. The colours are the ones
 * the charts use for passed and failed.
 */
function Finding({ finding }: { finding: RubricFinding }) {
  if (!finding.expected && !finding.actual) {
    return <p className={textStyle.meta}>{finding.fallback}</p>;
  }
  return (
    <dl className="grid grid-cols-[4.5rem_1fr] gap-x-2 gap-y-0.5">
      {finding.expected && (
        <>
          <dt className={cn(textStyle.label, "text-teal-400")}>Expected</dt>
          <dd className={textStyle.meta}>{finding.expected}</dd>
        </>
      )}
      {finding.actual && (
        <>
          <dt className={cn(textStyle.label, "text-orange-400")}>Actual</dt>
          <dd className={textStyle.meta}>{finding.actual}</dd>
        </>
      )}
    </dl>
  );
}

export function formatBugReport({
  insight,
  occurrences,
  rootCause,
  origin = "",
}: {
  insight: InsightView;
  occurrences: InsightOccurrence[];
  /** Root-cause record to include in the bug report. */
  rootCause?: RootCause;
  origin?: string;
}): string {
  const permalink = `${origin}/insights/${encodeURIComponent(insight.insight_id)}`;
  // The heading's rule, so the report is filed under the name the pane shows.
  // The diagnosis describes the behaviour in 30-70 words and has its own
  // section below.
  const title = insight.refined_label || insight.label;
  const latest = occurrences[0];
  const cases = latest?.evidence_case_ids ?? [];
  // Goes into a bug someone else reads, so it must not date the finding: an
  // issue with no root cause is one nobody has diagnosed, which says nothing
  // about when it was recorded.
  const diagnosisText =
    insight.diagnosis ||
    rootCause?.summary ||
    "No root cause recorded: the judge model's observation, not a diagnosis.";
  const lines: string[] = [
    `# [Bug Report] ${title}`,
    "",
    `**Observed agent:** ${insight.agent_name || "unknown"}`,
    `**Status:** ${insight.status}`,
    `**Impact:** ${insight.impact}`,
    `**Affected Trajectories:** ${insight.trace_count} across ${insight.occurrence_count} ${insight.occurrence_count === 1 ? "occurrence" : "occurrences"}`,
    `**Permalink:** ${permalink}`,
    "",
    "## Diagnosis",
    "",
    diagnosisText,
    "",
  ];

  // Against `title`, not the diagnosis: the heading prefers `refined_label`,
  // so comparing to the diagnosis repeats a label the report already shows.
  if (insight.label && insight.label !== title) {
    lines.push(`**Triage Signature:** ${insight.label}`, "");
  }

  if (cases.length > 0) {
    lines.push("## Trajectories", "");
    for (const caseId of cases) {
      lines.push(`- \`${caseId}\``);
    }
    lines.push("");
  }

  return lines.join("\n");
}

export function InsightDetailView({ insightId }: { insightId: string }) {
  const { data, isLoading, isError, error } = useQuery(insightQuery(insightId));
  const [copied, setCopied] = useState(false);

  if (isLoading) return <LoadingPane label="Loading insight…" />;
  if (isError)
    return (
      <Muted>
        <FailureNote lead="Couldn't load this insight" error={error} />
      </Muted>
    );
  if (!data) return <Muted>No insight {insightId}.</Muted>;

  const { insight, occurrences } = data;
  const [latest, ...older] = occurrences;
  // Prefer top-level insight records to include diagnoses across all occurrences,
  // falling back to occurrence-level records.
  const records =
    data.root_causes.length > 0
      ? data.root_causes
      : occurrences.flatMap((o) => o.root_causes ?? []);
  const currentRecord = newestRootCause(records);
  // `occurrence_count` is every sighting on record; `occurrences` is the capped
  // page. A `next_page_token` is the engine saying it held back more.
  const recorded = Math.max(insight.occurrence_count || 0, occurrences.length);
  const unshown = data.next_page_token
    ? Math.max(recorded - occurrences.length, 1)
    : 0;

  const handleCopyReport = async () => {
    const origin = typeof window !== "undefined" ? window.location.origin : "";
    const text = formatBugReport({
      insight,
      occurrences,
      rootCause: currentRecord,
      origin,
    });
    try {
      if (navigator.clipboard) {
        await navigator.clipboard.writeText(text);
        setCopied(true);
        setTimeout(() => setCopied(false), 2500);
      }
    } catch {
      /* ignore clipboard rejection */
    }
  };

  return (
    <div className="flex flex-col gap-5">
      <div className="flex items-center justify-between">
        <Link
          to="/insights"
          className={cn(
            textStyle.meta,
            "inline-flex w-fit items-center gap-1 hover:text-foreground",
          )}
        >
          <ArrowLeft className="h-3 w-3" /> All insights
        </Link>

        <div data-tour="insight-actions" className="flex items-center gap-2">
          <DiagnoseInChatButton
            insightId={insight.insight_id}
            label={insight.label}
            ariaLabel={`Diagnose insight in chat: ${insight.refined_label || insight.label}`}
            variant="default"
            className="h-7 gap-1.5 font-normal [&_svg]:size-3.5"
          />

          <Button
            size="sm"
            variant="outline"
            onClick={handleCopyReport}
            className="h-7 gap-1.5 font-normal"
            title="Copy formatted markdown report to clipboard"
          >
            {copied ? (
              <>
                <Check className="h-3.5 w-3.5 text-emerald-500" /> Copied!
              </>
            ) : (
              <>
                <Copy className="h-3.5 w-3.5" /> Copy as bug report
              </>
            )}
          </Button>
        </div>
      </div>

      {/* Render the triage label as the heading to keep it distinct from
            the autorater diagnosis and recorded root cause. */}
      <PageHeader
        title={insight.refined_label || insight.label}
        className="gap-2"
      >
        <div
          className={cn(textStyle.meta, "flex flex-wrap items-center gap-3")}
        >
          {/* No confidence: nothing in this pipeline measures one, and a
                percentage nobody computes is worse than no percentage. The
                sparkline and the "vs previous" delta went with it -- both were
                plots of that number. */}
          {!isDiagnosed(insight) && <span>Undiagnosed</span>}
          <span>{formatSentenceCase(insight.status.toLowerCase())}</span>
          <span>
            {insight.occurrence_count}{" "}
            {insight.occurrence_count === 1 ? "occurrence" : "occurrences"}
          </span>
          <span>
            {insight.trace_count}{" "}
            {insight.trace_count === 1 ? "trajectory" : "trajectories"}
          </span>
          <span>{insight.impact} impact</span>
        </div>
      </PageHeader>

      {currentRecord && (
        <section
          data-testid="root-cause-panel"
          className="flex flex-col gap-2 rounded-lg border p-3"
        >
          <h2 className={textStyle.label}>Root cause</h2>
          <div className={cn(textStyle.meta, "flex flex-wrap gap-3")}>
            <span>{new Date(currentRecord.created_at).toLocaleString()}</span>
            {currentRecord.agent_revision && (
              <span className={mono}>{currentRecord.agent_revision}</span>
            )}
          </div>
          <RootCauseRecord
            record={currentRecord}
            note={
              records.length > 1
                ? `Showing the latest of ${records.length} records.`
                : undefined
            }
          />
        </section>
      )}

      {latest && (
        <div data-tour="insight-evidence">
          <Occurrence
            occurrence={latest}
            runId={latest.run_id || insight.last_run_id || "latest"}
            heading="Latest occurrence"
          />
        </div>
      )}

      {older.length > 0 && (
        <section className="flex flex-col gap-2">
          <h2 className={textStyle.label}>
            History ({occurrences.length} of {recorded})
          </h2>
          {older.map((o) => (
            <Occurrence
              key={o.occurrence_id}
              occurrence={o}
              runId={o.run_id || insight.last_run_id || "latest"}
              compact
            />
          ))}
          {/* Evidence that is missing without saying so is worse than
                evidence that is missing. Nothing here requests the next page,
                so the count is the whole answer. */}
          {unshown > 0 && (
            <p className={textStyle.meta}>
              {unshown.toLocaleString()} earlier{" "}
              {unshown === 1 ? "occurrence is" : "occurrences are"} not shown.
            </p>
          )}
        </section>
      )}

      {/* The whole payload, occurrences included — the rubrics and evidence
            ids the cards summarise are only fully legible here. */}
      <RawJson label="Raw insight" value={data} />
    </div>
  );
}

export function Occurrence({
  occurrence,
  heading,
  compact,
}: {
  occurrence: InsightOccurrence;
  runId: string;
  heading?: string;
  compact?: boolean;
}) {
  return (
    <section className="flex flex-col gap-2 rounded-lg border p-3">
      {heading && <h2 className={textStyle.label}>{heading}</h2>}
      <div className={cn(textStyle.meta, "flex flex-wrap gap-3")}>
        {!isOccurrenceDiagnosed(occurrence) && <span>Undiagnosed</span>}
        <span>{new Date(occurrence.created_at).toLocaleString()}</span>
        <span>
          {occurrence.trace_count || occurrence.item_count}{" "}
          {(occurrence.trace_count || occurrence.item_count) === 1
            ? "trajectory"
            : "trajectories"}
        </span>
        {/* Where this finding came from. A `chat-` id is a conversation, not
            a run: linking it to /investigations sent the reader to a run that
            never existed. */}
        {occurrence.run_id &&
          isChatRun(occurrence.run_id) &&
          (chatContextId(occurrence.run_id) ? (
            <Link
              to="/c"
              search={{ id: chatContextId(occurrence.run_id) as string }}
              className={cn(
                link.standalone,
                "inline-flex items-center gap-1 text-primary",
              )}
            >
              <MessageSquare className="h-3 w-3" />
              Open the chat
            </Link>
          ) : (
            // No link: the conversation this came from is not known, and a
            // link to nothing reads exactly like a link to something.
            <span
              className="inline-flex items-center gap-1 text-muted-foreground"
              title="Recorded from a chat that was not identified."
            >
              <MessageSquare className="h-3 w-3" />
              Recorded in chat
            </span>
          ))}
        {occurrence.run_id && !isChatRun(occurrence.run_id) && (
          <Link
            to="/investigations/$runId"
            params={{ runId: occurrence.run_id }}
            className={cn(
              link.standalone,
              "inline-flex items-center gap-1 text-primary",
            )}
          >
            <Search className="h-3 w-3" />
            Investigation {occurrence.run_id.slice(0, 8)}
          </Link>
        )}
      </div>
      {/* `compact` drops what repeats across sightings -- the conclusion and
          the notice about it -- and keeps what does not. */}
      {!compact && occurrence.diagnosis && (
        <p className={textStyle.body}>{occurrence.diagnosis}</p>
      )}
      {!compact && !isOccurrenceDiagnosed(occurrence) && (
        <p className={textStyle.meta}>
          This occurrence has no recorded root cause.
        </p>
      )}
      {/* This sighting's own evidence: what failed in this sweep, as opposed to
          the diagnosis drawn from all of them.

          No bullets on the list: each finding is a two-row block, and a disc
          beside the first row would sit against "expected" and read as its
          marker. */}
      {occurrence.rubrics && occurrence.rubrics.length > 0 && (
        <ul className="space-y-2">
          {occurrence.rubrics.map((r, idx) => (
            // biome-ignore lint/suspicious/noArrayIndexKey: rubric findings carry no ids and render in the order the sweep stored them.
            <li key={idx}>
              <Finding finding={rubricFinding(r)} />
            </li>
          ))}
        </ul>
      )}
      {occurrence.evidence_case_ids.length > 0 && (
        <div className="flex flex-col gap-1">
          <span className={textStyle.label}>Trajectories</span>
          {/* The chip opens the archived conversation; the icon beside it opens
              the raw trace in Cloud Trace. Two different questions -- what the
              agent said, and what the spans recorded -- so both are offered,
              and the trace one only where the sighting carries trace ids
              (`big_query` telemetry carries none). The case page reports a
              conversation the archive does not hold, so a chip is never a dead
              end even though this cannot know in advance. */}
          <ul className="flex flex-wrap gap-1.5">
            {occurrence.evidence_case_ids.slice(0, 8).map((caseId) => {
              const trace = occurrence.console_urls?.[caseId];
              const runId = occurrence.run_id;
              const chip = (
                <>
                  <MessageSquare className="h-3 w-3" />
                  {caseId.slice(0, 12)}
                </>
              );
              const chipClass = cn(
                textStyle.meta,
                mono,
                "inline-flex items-center gap-1 rounded border px-1.5 py-0.5",
              );
              return (
                <li key={caseId} className="inline-flex items-center gap-1">
                  {/* The replay lives under a run, so a sighting that records
                      none has an id worth showing and nowhere to send it. */}
                  {runId ? (
                    <Link
                      to="/investigations/$runId/cases/$caseId"
                      params={{ runId, caseId }}
                      title={`Replay ${caseId}`}
                      className={`${chipClass} transition-colors hover:border-foreground/40 hover:text-foreground`}
                    >
                      {chip}
                    </Link>
                  ) : (
                    <span className={chipClass}>{chip}</span>
                  )}
                  {trace && (
                    <a
                      href={trace}
                      target="_blank"
                      rel="noreferrer"
                      title={`Open ${caseId} in Cloud Trace`}
                      className="text-muted-foreground transition-colors hover:text-foreground"
                    >
                      <ExternalLink className="h-3 w-3" />
                    </a>
                  )}
                </li>
              );
            })}
          </ul>
        </div>
      )}
    </section>
  );
}

function Muted({ children }: { children: React.ReactNode }) {
  return <div className={textStyle.description}>{children}</div>;
}
