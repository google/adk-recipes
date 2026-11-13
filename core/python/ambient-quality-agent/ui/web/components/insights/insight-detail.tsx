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
import { Link, useLocation, useNavigate } from "@tanstack/react-router";
import {
  ArrowLeft,
  Check,
  Copy,
  ExternalLink,
  MessageSquare,
  Search,
} from "lucide-react";

/** Findings recorded straight from a conversation carry `chat-<contextId>`
 *  rather than an investigation id; they have no investigation page and no
 *  sweep artifact behind them. */
const CHAT_INVESTIGATION_PREFIX = "chat-";
/** Recorded in a chat we cannot name, so there is nothing to link to. */
const UNATTRIBUTED = "unattributed";
const isChatInvestigation = (investigationId: string) =>
  investigationId.startsWith(CHAT_INVESTIGATION_PREFIX);
const chatContextId = (investigationId: string): string | null => {
  const id = investigationId.slice(CHAT_INVESTIGATION_PREFIX.length);
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
import { stripMarkdownForSummary } from "@/lib/aqa-markdown";
import { resolveSubAgent } from "@/lib/insight-agent";
import { Button } from "@/components/ui/button";
import { InlineMarkdown } from "@/components/ui/inline-markdown";
import { DiagnoseInChatButton } from "./diagnose-in-chat-button";
import { SubAgentItem } from "./sub-agent-item";
import { RootCauseRecord, newestRootCause } from "./root-cause-record";
import { cn, formatSentenceCase } from "@/lib/utils";
import { focusRing, link, mono, textStyle } from "@/lib/typography";
import { PageHeader } from "@/components/nav/page-header";
import { FailureNote } from "@/components/ui/failure-note";
import { LoadingPane } from "@/components/ui/loading";
import { RawJson } from "@/components/ui/raw-json";
import { RememberedDetails } from "@/components/ui/remembered-details";
import { useAgentKey, buildAgentHref } from "@/lib/agent-selection";

/** One sampled finding: the two behaviours, kept apart. */
export interface ExampleFinding {
  expected: string;
  actual: string;
  /** Used only when neither behaviour is readable, so the row still points at
   *  something rather than printing the object. */
  fallback: string;
}

/** Read one sampled finding off one of a sighting's examples.
 *
 * `review` asks the model for both behaviours and drops a finding carrying
 * neither, so one of the two can be absent and both usually are not.
 *
 * Shapes are read defensively rather than typed: this payload crosses from a
 * sweep's own record straight into the pane, and an example that does not match
 * is worth showing as its id rather than as a stack trace.
 */
export function readExampleFinding(
  raw: Record<string, unknown>,
): ExampleFinding {
  const finding = (raw.finding ?? {}) as Record<string, unknown>;
  const text = (k: string, from: Record<string, unknown>): string =>
    typeof from[k] === "string" ? (from[k] as string).trim() : "";

  const actual =
    text("actual_behavior", finding) || text("actual_behavior", raw);
  const expected =
    text("expected_behavior", finding) || text("expected_behavior", raw);

  const id =
    text("trajectory_id", raw) ||
    text("trajectory_id", finding) ||
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
 * the charts use for passed and failed, a darker shade on the light theme so
 * the labels stay readable.
 */
function Finding({ finding }: { finding: ExampleFinding }) {
  if (!finding.expected && !finding.actual) {
    return <p className={textStyle.meta}>{finding.fallback}</p>;
  }
  return (
    <dl className="grid grid-cols-[4.5rem_1fr] gap-x-2 gap-y-0.5">
      {finding.expected && (
        <>
          <dt
            className={cn(textStyle.label, "text-teal-700 dark:text-teal-400")}
          >
            Expected
          </dt>
          <dd className={textStyle.meta}>
            <InlineMarkdown text={finding.expected} />
          </dd>
        </>
      )}
      {finding.actual && (
        <>
          <dt
            className={cn(
              textStyle.label,
              "text-orange-700 dark:text-orange-400",
            )}
          >
            Actual
          </dt>
          <dd className={textStyle.meta}>
            <InlineMarkdown text={finding.actual} />
          </dd>
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
  agentKey,
}: {
  insight: InsightView;
  occurrences: InsightOccurrence[];
  /** Root-cause record to include in the bug report. */
  rootCause?: RootCause;
  origin?: string;
  /** The selected agent, which the permalink opens on. */
  agentKey?: string;
}): string {
  const permalink = `${origin}${buildAgentHref(
    `/insights/${encodeURIComponent(insight.insight_id)}`,
    agentKey,
  )}`;
  // The heading's rule, so the report is filed under the name the pane shows.
  // The diagnosis describes the behaviour in 30-70 words and has its own
  // section below.
  const title = insight.refined_label || insight.label;
  const latest = occurrences[0];
  const cases = latest?.evidence_trajectory_ids ?? [];
  // Goes into a bug someone else reads, so it must not date the finding: an
  // issue with no root cause is one nobody has diagnosed, which says nothing
  // about when it was recorded.
  const diagnosisText =
    insight.diagnosis ||
    rootCause?.summary ||
    "No root cause recorded: the judge model's observation, not a diagnosis.";
  const subAgent = resolveSubAgent(insight);
  const lines: string[] = [
    `# [Bug Report] ${title}`,
    "",
    `**Observed agent:** ${insight.observed_agent_key || "unknown"}`,
    ...(subAgent ? [`**Sub-agent:** ${subAgent}`] : []),
    `**Status:** ${insight.status}`,
    `**Impact:** ${insight.impact}`,
    `**Affected Trajectories:** ${insight.trajectory_count} across ${insight.occurrence_count} ${insight.occurrence_count === 1 ? "occurrence" : "occurrences"}`,
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
    for (const trajectoryId of cases) {
      lines.push(`- \`${trajectoryId}\``);
    }
    lines.push("");
  }

  return lines.join("\n");
}

export function InsightDetailView({ insightId }: { insightId: string }) {
  const agentKey = useAgentKey();
  const { data, isLoading, isError, error } = useQuery(
    insightQuery(agentKey, insightId),
  );
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
      agentKey,
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
        <BackToListLink />

        <div data-tour="insight-actions" className="flex items-center gap-2">
          <DiagnoseInChatButton
            insightId={insight.insight_id}
            label={insight.label}
            ariaLabel={`Diagnose insight in chat: ${stripMarkdownForSummary(insight.refined_label || insight.label)}`}
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
        title={<InlineMarkdown text={insight.refined_label || insight.label} />}
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
            {insight.trajectory_count}{" "}
            {insight.trajectory_count === 1 ? "trajectory" : "trajectories"}
          </span>
          <SubAgentItem insight={insight} />
          <span>{insight.impact} impact</span>
        </div>
      </PageHeader>

      <InsightSummary
        diagnosis={insight.diagnosis || latest?.diagnosis || ""}
        record={currentRecord}
        findings={(latest?.examples ?? []).map(readExampleFinding)}
      />

      {latest && (
        <div data-tour="insight-evidence">
          <Occurrence
            occurrence={latest}
            investigationId={
              latest.investigation_id ||
              insight.last_investigation_id ||
              "latest"
            }
            heading="Latest occurrence"
            hideFindings
          />
        </div>
      )}

      {currentRecord && (
        <section
          data-testid="root-cause-panel"
          className="flex flex-col gap-2 rounded-lg border p-3"
        >
          <h2 className={textStyle.label}>Proposed code changes</h2>
          <div className={cn(textStyle.meta, "flex flex-wrap gap-3")}>
            <span>{new Date(currentRecord.created_at).toLocaleString()}</span>
            {currentRecord.agent_revision && (
              <span className={mono}>{currentRecord.agent_revision}</span>
            )}
          </div>
          <RootCauseRecord
            record={currentRecord}
            hideSummary
            note={
              records.length > 1
                ? `Showing the latest of ${records.length} records.`
                : undefined
            }
          />
        </section>
      )}

      {/* Folded: each earlier sighting lists its own findings for the same
          defect, and a reader who opens the page for the summary should not
          scroll past them. A reader who opens it keeps it open. */}
      {older.length > 0 && (
        <RememberedDetails
          storageKey="aqua.insight.history"
          summaryClassName={disclosure}
          summary={
            <h2 className={cn(textStyle.label, "inline")}>
              History ({occurrences.length} of {recorded})
            </h2>
          }
        >
          <div className="mt-2 flex flex-col gap-2">
            {older.map((o) => (
              <Occurrence
                key={o.occurrence_id}
                occurrence={o}
                investigationId={
                  o.investigation_id ||
                  insight.last_investigation_id ||
                  "latest"
                }
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
          </div>
        </RememberedDetails>
      )}

      {/* The whole payload, occurrences included — the finding examples and
            evidence trajectory IDs the cards summarise are only fully legible
            here. */}
      <RawJson label="Raw insight" value={data} />
    </div>
  );
}

/** A `<summary>` that looks clickable and shows the app's focus ring. */
const disclosure = cn("cursor-pointer hover:text-foreground", focusRing);

/**
 * What a reader needs to act on the insight without reading code or traces:
 * the defect, its cause, what to change, and one finding that shows it.
 *
 * The cause and the fix come from the newest recorded root cause: its summary,
 * and the one-line rationale of each edit it proposes, whose diffs follow
 * further down the page. Only a chat turn records one, so an insight without
 * one says how to get it.
 */
function InsightSummary({
  diagnosis,
  record,
  findings,
}: {
  diagnosis: string;
  record: RootCause | undefined;
  findings: ExampleFinding[];
}) {
  const [example, ...rest] = findings;
  const edits = record?.edits ?? [];
  const rationales = edits.flatMap((edit) => edit.rationale || []);
  return (
    <section
      aria-labelledby="insight-summary-heading"
      className="flex flex-col gap-3 rounded-lg border p-3"
    >
      <h2 id="insight-summary-heading" className="sr-only">
        Summary
      </h2>
      <SummaryField
        label="What's wrong"
        text={diagnosis}
        empty="Nothing recorded yet."
      />
      <SummaryField
        label="Root cause"
        text={record?.summary ?? ""}
        empty={
          !record
            ? "No root cause recorded yet: what's wrong above is what the judge model saw, not why it happened. Click Chat to find the cause and propose a fix."
            : edits.length > 0
              ? "Recorded without a summary. The proposed code changes are below."
              : "Recorded without a summary or code changes."
        }
      />
      {rationales.length > 0 && (
        <div className="flex flex-col gap-1">
          <h3 className={textStyle.label}>Suggested fix</h3>
          <ul className={cn(textStyle.body, "list-disc space-y-0.5 pl-5")}>
            {rationales.map((rationale, idx) => (
              // biome-ignore lint/suspicious/noArrayIndexKey: two edits can share a rationale, and the list never reorders.
              <li key={idx}>
                <InlineMarkdown text={rationale} />
              </li>
            ))}
          </ul>
        </div>
      )}
      {example && (
        <div className="flex flex-col gap-1">
          <h3 className={textStyle.label}>Example finding</h3>
          <Finding finding={example} />
          {rest.length > 0 && (
            <RememberedDetails
              storageKey="aqua.insight.more-findings"
              summaryClassName={cn(textStyle.meta, disclosure)}
              summary={`Show ${rest.length} more ${rest.length === 1 ? "finding" : "findings"}`}
            >
              <div className="mt-2">
                <FindingList findings={rest} />
              </div>
            </RememberedDetails>
          )}
        </div>
      )}
    </section>
  );
}

/** One labelled line of the summary, or a muted note when it has no text. */
function SummaryField({
  label,
  text,
  empty,
}: {
  label: string;
  text: string;
  empty: string;
}) {
  return (
    <div className="flex flex-col gap-1">
      <h3 className={textStyle.label}>{label}</h3>
      <p className={text ? textStyle.body : textStyle.meta}>
        {text ? <InlineMarkdown text={text} /> : empty}
      </p>
    </div>
  );
}

/** Findings in the order the sweep stored them.
 *
 * No bullets: each finding is a two-row block, and a disc beside the first
 * row would sit against "expected" and read as its marker.
 */
function FindingList({ findings }: { findings: ExampleFinding[] }) {
  return (
    <ul className="space-y-2">
      {findings.map((finding, idx) => (
        // biome-ignore lint/suspicious/noArrayIndexKey: findings carry no ids and render in the order the sweep stored them.
        <li key={idx}>
          <Finding finding={finding} />
        </li>
      ))}
    </ul>
  );
}

export function Occurrence({
  occurrence,
  heading,
  hideFindings,
}: {
  occurrence: InsightOccurrence;
  investigationId: string;
  heading?: string;
  /** Leave out the findings, for the sighting the summary already shows. */
  hideFindings?: boolean;
}) {
  return (
    <section className="flex flex-col gap-2 rounded-lg border p-3">
      {heading && <h2 className={textStyle.label}>{heading}</h2>}
      <div className={cn(textStyle.meta, "flex flex-wrap gap-3")}>
        {!isOccurrenceDiagnosed(occurrence) && <span>Undiagnosed</span>}
        <span>{new Date(occurrence.created_at).toLocaleString()}</span>
        <span>
          {occurrence.trajectory_count || occurrence.finding_count}{" "}
          {(occurrence.trajectory_count || occurrence.finding_count) === 1
            ? "trajectory"
            : "trajectories"}
        </span>
        {/* Where this finding came from. A `chat-` id is a conversation, not an
            investigation: linking it to /investigations sent the reader to an
            investigation that never existed. */}
        {occurrence.investigation_id &&
          isChatInvestigation(occurrence.investigation_id) &&
          (chatContextId(occurrence.investigation_id) ? (
            <Link
              to="/c"
              search={{
                id: chatContextId(occurrence.investigation_id) as string,
              }}
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
        {occurrence.investigation_id &&
          !isChatInvestigation(occurrence.investigation_id) && (
            <Link
              to="/investigations/$investigationId"
              params={{ investigationId: occurrence.investigation_id }}
              className={cn(
                link.standalone,
                "inline-flex items-center gap-1 text-primary",
              )}
            >
              <Search className="h-3 w-3" />
              Investigation {occurrence.investigation_id.slice(0, 8)}
            </Link>
          )}
      </div>
      {/* This sighting's own evidence: what failed in this sweep. The summary
          shows the latest sighting's findings, so its card leaves them out. */}
      {!hideFindings &&
        occurrence.examples &&
        occurrence.examples.length > 0 && (
          <FindingList findings={occurrence.examples.map(readExampleFinding)} />
        )}
      {occurrence.evidence_trajectory_ids.length > 0 && (
        <div className="flex flex-col gap-1">
          <span className={textStyle.label}>Trajectories</span>
          {/* The chip opens the trajectory bundle; the icon beside it opens
              the raw trace in Cloud Trace. Two different questions -- what the
              agent said, and what the spans recorded -- so both are offered,
              and the trace one only where the sighting carries trace ids
              (`big_query` telemetry carries none). The trajectory page reports a
              trajectory the store does not hold, so a chip is never a dead
              end even though this cannot know in advance. */}
          <ul className="flex flex-wrap gap-1.5">
            {occurrence.evidence_trajectory_ids
              .slice(0, 8)
              .map((trajectoryId) => {
                const trace = occurrence.console_urls?.[trajectoryId];
                const investigationId = occurrence.investigation_id;
                const chip = (
                  <>
                    <MessageSquare className="h-3 w-3" />
                    {trajectoryId.slice(0, 12)}
                  </>
                );
                const chipClass = cn(
                  textStyle.meta,
                  mono,
                  "inline-flex items-center gap-1 rounded border px-1.5 py-0.5",
                );
                return (
                  <li
                    key={trajectoryId}
                    className="inline-flex items-center gap-1"
                  >
                    {/* The replay lives under an investigation, so a sighting
                      that records none has an id worth showing and nowhere to
                      send it. */}
                    {investigationId ? (
                      <Link
                        to="/investigations/$investigationId/trajectories/$trajectoryId"
                        params={{
                          investigationId: investigationId,
                          trajectoryId,
                        }}
                        title={`Replay ${trajectoryId}`}
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
                        title={`Open ${trajectoryId} in Cloud Trace`}
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

/**
 * The way back to the list the insight was opened from.
 *
 * The list records its own address, page and status included, in the history
 * entry it opens the insight with, so this returns to the page the reader was
 * on rather than the top of the list. Without that record, as on a shared link
 * or a reload in a new tab, it goes to the whole insights list.
 */
function BackToListLink() {
  const listHref = useLocation({
    select: (location) => location.state?.insightsListHref,
  });
  const navigate = useNavigate();
  const className = cn(
    textStyle.meta,
    "inline-flex w-fit items-center gap-1 hover:text-foreground",
  );
  if (!listHref) {
    return (
      <Link to="/insights" className={className}>
        <ArrowLeft className="h-3 w-3" /> All insights
      </Link>
    );
  }
  return (
    <a
      href={listHref}
      className={className}
      onClick={(e) => {
        // A modified click opens the list in a new tab, which the browser does
        // from the href on its own.
        if (e.metaKey || e.ctrlKey || e.shiftKey || e.altKey || e.button !== 0)
          return;
        e.preventDefault();
        void navigate({ href: listHref });
      }}
    >
      <ArrowLeft className="h-3 w-3" />{" "}
      {listHref.startsWith("/investigations")
        ? "Back to investigation"
        : "All insights"}
    </a>
  );
}
