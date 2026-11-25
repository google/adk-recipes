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
import {
  healthQuery,
  type AmbientHealth,
  type AmbientVerdict,
} from "@/lib/aqua-api";
import { stripMarkdownForSummary } from "@/lib/aqa-markdown";
import { FailureNote } from "@/components/ui/failure-note";
import { HelpPopover } from "@/components/ui/help-popover";
import { formatCron } from "@/lib/cron";
import { ESTIMATE_HELP } from "@/lib/estimates";
import { POLL_INTERVAL_MS, renderOngoingNote } from "@/lib/investigation-state";
import { formatInstant } from "@/lib/investigation-summary";
import { textStyle } from "@/lib/typography";
import { cn, formatSentenceCase } from "@/lib/utils";
import { useAgentKey } from "@/lib/agent-selection";
import { isViewerMode } from "@/lib/viewer-mode";

const VERDICT: Record<AmbientVerdict, { value: string; tone: string }> = {
  watching: { value: "Watching", tone: "text-emerald-400" },
  overdue: { value: "Overdue", tone: "text-amber-400" },
  never: { value: "Never run", tone: "text-amber-400" },
  failing: { value: "Failing", tone: "text-red-400" },
  disabled: { value: "Disabled", tone: "text-amber-400" },
};

const ago = (iso: string | null): string => {
  if (!iso) return "unknown";
  const hours = (Date.now() - new Date(iso).getTime()) / 3_600_000;
  if (hours < 1) return "just now";
  if (hours < 24) return `${Math.floor(hours)}h ago`;
  return `${Math.floor(hours / 24)}d ago`;
};

function Cell({
  label,
  value,
  note,
  help,
  wrapNote = false,
  tone,
  to,
  params,
}: {
  label: string;
  value: string;
  note: string;
  /** Explains the value behind a help button beside the label. Not for a cell
   *  with `to`, since a button inside a link is invalid. */
  help?: string;
  /** Lets the note take a second line rather than cutting it short. */
  wrapNote?: boolean;
  tone?: string;
  to?: string;
  params?: Record<string, string>;
}) {
  const body = (
    <>
      <div className="flex items-center gap-1">
        <p className={textStyle.label}>{label}</p>
        {help && <HelpPopover topic={label}>{help}</HelpPopover>}
      </div>
      <p className={cn(textStyle.pageTitle, "mt-1", tone)}>{value}</p>
      <p
        className={cn(
          textStyle.meta,
          "mt-1",
          wrapNote ? "line-clamp-2" : "truncate",
        )}
        title={note}
      >
        {note}
      </p>
    </>
  );
  return (
    <div className="min-w-0 flex-1 px-5 py-4 first:pl-0 last:pr-0">
      {to ? (
        <Link
          // biome-ignore lint/suspicious/noExplicitAny: `to` and `params` are typed per route, and this cell links to any route.
          to={to as any}
          // biome-ignore lint/suspicious/noExplicitAny: `to` and `params` are typed per route, and this cell links to any route.
          params={params as any}
          className="block hover:opacity-80"
        >
          {body}
        </Link>
      ) : (
        body
      )}
    </div>
  );
}

const formatPercent = (rate: number): string => `${Math.round(rate * 100)}%`;

/** What the pass rate is a share of: the trajectories that reached a verdict,
 *  and beside them the evaluations the eval service errored on. Those are no
 *  judgement either way, so they stay out of the rate and are named alongside
 *  it -- a sweep the reviewer model was unavailable for found nothing wrong
 *  with the agent, and a note reading "0 of 50" would say it did.
 *
 *  An engine that predates those two counts sends neither, and its
 *  `pass_rate` is a share of every evaluated trajectory; the note says so,
 *  rather than pairing that engine's rate with a denominator it did not use.
 *  An absent count is not a null one: a current engine sends null for a count
 *  of zero, which the judged branch below reads. */
function formatPassRateNote(
  investigation: AmbientHealth["last_investigation"],
): string {
  const evaluated = investigation?.trajectories ?? 0;
  if (!evaluated) return "No trajectories evaluated";
  if (investigation?.trajectories_judged === undefined) {
    const passed = investigation?.trajectories_passed ?? 0;
    const unit = evaluated === 1 ? "trajectory" : "trajectories";
    return `${passed} of ${evaluated} ${unit}`;
  }
  const judged = investigation.trajectories_judged ?? 0;
  const errored = investigation.trajectories_errored ?? 0;
  const errors = errored > 0 ? ` · ${errored} errored` : "";
  if (!judged) return `No trajectory judged${errors}`;
  return `${investigation.trajectories_passed ?? 0} of ${judged} judged${errors}`;
}

/** The latest investigation's pass rate: estimated for all traffic when the
 *  investigation recorded estimates, with the sample it was estimated from
 *  beneath; the sample's own rate otherwise. A weighted sample's rate
 *  over-represents failures, so once there is an estimate it appears only in
 *  the help. */
function PassRateCell({
  investigation,
}: {
  investigation: AmbientHealth["last_investigation"];
}) {
  const estimated = investigation?.estimated_pass_rate;
  if (investigation && estimated != null) {
    const reviewed = investigation.trajectories ?? 0;
    const picked = investigation.trajectories_with_risk_signals ?? 0;
    const scanned = investigation.trajectories_scanned;
    const parts = [
      // "Conversations" is left to the help: two lines of a quarter of the bar
      // hold both figures only without it.
      scanned != null
        ? `Reviewed ${reviewed.toLocaleString()} of ${scanned.toLocaleString()}`
        : `Reviewed ${reviewed.toLocaleString()}`,
    ];
    if (picked > 0) {
      parts.push(`${picked.toLocaleString()} picked for risk signals`);
    }
    // The sample's rate is a share of the conversations that reached a
    // verdict, which is fewer than the conversations reviewed once an
    // evaluation errored. An engine that reports no judged count rated every
    // conversation it reviewed.
    const judged = investigation.trajectories_judged;
    const rated =
      judged === undefined
        ? `The ${reviewed.toLocaleString()} conversations reviewed`
        : `The ${(judged ?? 0).toLocaleString()} judged conversations`;
    const sample =
      investigation.pass_rate != null
        ? ` ${rated} passed at ${formatPercent(investigation.pass_rate)}.`
        : "";
    return (
      <Cell
        label="Estimated pass rate"
        value={formatPercent(estimated)}
        help={`${ESTIMATE_HELP}${sample}`}
        note={parts.join(" · ")}
        wrapNote
      />
    );
  }
  return (
    <Cell
      label="Pass rate"
      value={
        investigation?.pass_rate != null
          ? formatPercent(investigation.pass_rate)
          : "--"
      }
      note={formatPassRateNote(investigation)}
    />
  );
}

/**
 * The homepage's first answer: is AQuA working, and what has it found.
 *
 * Every value is read straight from BigQuery with no model in the path. "Your
 * scheduler has never fired" is not a sentence to leave to a model's choice of
 * words, and it is the sentence this bar exists to say.
 */
export function StatBar() {
  const agentKey = useAgentKey();
  const viewer = isViewerMode();
  const { data, isLoading, error } = useQuery({
    ...healthQuery(agentKey),
    refetchInterval: (query) =>
      !viewer && (query.state.data?.ongoing_investigations?.length ?? 0) > 0
        ? POLL_INTERVAL_MS
        : false,
  });

  if (isLoading) {
    return (
      <div className="h-[104px] animate-pulse rounded-xl border bg-muted/20" />
    );
  }

  // An unreachable health check is its own state. Rendering a row of zeroes
  // here would be the confident-absence bug this codebase keeps shipping, on
  // the one surface whose whole job is to be true.
  if (error || !data || data.error) {
    return (
      <div className="rounded-xl border border-dashed px-5 py-6">
        <FailureNote
          lead="Health unavailable"
          error={error ?? data?.error ?? "no response"}
        />
      </div>
    );
  }

  const verdict = VERDICT[data.verdict] ?? {
    // An unknown verdict from a newer engine is unknown, not broken: guessing
    // the worst is the same error as guessing the best.
    value: data.verdict,
    tone: "text-muted-foreground",
  };
  const investigation = data.last_investigation;
  const healthy = data.verdict === "watching";
  const ongoingText = renderOngoingNote(
    data.ongoing_investigations,
    formatInstant,
  );
  const scheduleText = data.schedule ? formatCron(data.schedule) : null;
  const ambientNote = ongoingText
    ? scheduleText
      ? `${ongoingText} · ${scheduleText}`
      : ongoingText
    : (scheduleText ?? data.reason);

  return (
    <section
      aria-label="Ambient health"
      className={cn(
        "flex flex-wrap divide-x divide-border/60 rounded-xl border px-5",
        !healthy && !viewer && "border-amber-500/30 bg-amber-500/[0.03]",
      )}
    >
      {!viewer && (
        <Cell
          label="Ambient"
          value={verdict.value}
          tone={verdict.tone}
          note={ambientNote}
          // Where the schedule is edited.
          to="/config"
        />
      )}
      <PassRateCell investigation={investigation} />
      <Cell
        label="Open insights"
        value={String(data.open_findings)}
        note={stripMarkdownForSummary(
          data.worst_finding?.label ?? "Nothing outstanding",
        )}
        to="/insights"
      />
      <Cell
        label="Last investigation"
        value={
          investigation
            ? formatSentenceCase(ago(investigation.finished_at))
            : "Never"
        }
        note={
          investigation
            ? formatSentenceCase(
                `${investigation.trigger_type} investigation ${investigation.investigation_id}`,
              )
            : "No investigation has run"
        }
        {...(investigation
          ? {
              to: "/investigations/$investigationId",
              params: { investigationId: investigation.investigation_id },
            }
          : {})}
      />
    </section>
  );
}
