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
import { healthQuery, type AmbientVerdict } from "@/lib/aqua-api";
import { FailureNote } from "@/components/ui/failure-note";
import { textStyle } from "@/lib/typography";
import { cn, formatSentenceCase } from "@/lib/utils";

const VERDICT: Record<AmbientVerdict, { value: string; tone: string }> = {
  watching: { value: "Watching", tone: "text-emerald-400" },
  overdue: { value: "Overdue", tone: "text-amber-400" },
  never: { value: "Never run", tone: "text-amber-400" },
  failing: { value: "Failing", tone: "text-red-400" },
  disabled: { value: "Disabled", tone: "text-amber-400" },
};

/**
 * "0 12 * * *" reads as glitched asterisks. Only the fixed-time daily case is
 * translated, which is the default and every schedule anyone has set; anything
 * else shows the crontab it actually is rather than a guess at its meaning.
 */
export function humanCron(cron: string): string {
  const daily = /^(\d{1,2}) (\d{1,2}) \* \* \*$/.exec(cron.trim());
  if (!daily) return cron;
  const [, minute, hour] = daily;
  return `daily at ${hour.padStart(2, "0")}:${minute.padStart(2, "0")} UTC`;
}

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
  tone,
  to,
  params,
}: {
  label: string;
  value: string;
  note: string;
  tone?: string;
  to?: string;
  params?: Record<string, string>;
}) {
  const body = (
    <>
      <p className={textStyle.label}>{label}</p>
      <p className={cn(textStyle.pageTitle, "mt-1", tone)}>{value}</p>
      <p className={cn(textStyle.meta, "mt-1 truncate")} title={note}>
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

/**
 * The homepage's first answer: is AQuA working, and what has it found.
 *
 * Every value is read straight from BigQuery with no model in the path. "Your
 * scheduler has never fired" is not a sentence to leave to a model's choice of
 * words, and it is the sentence this bar exists to say.
 */
export function StatBar() {
  const { data, isLoading, error } = useQuery(healthQuery());

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
  const run = data.last_run;
  const healthy = data.verdict === "watching";

  return (
    <section
      aria-label="Ambient health"
      className={cn(
        "flex flex-wrap divide-x divide-border/60 rounded-xl border px-5",
        !healthy && "border-amber-500/30 bg-amber-500/[0.03]",
      )}
    >
      <Cell
        label="Ambient"
        value={verdict.value}
        tone={verdict.tone}
        note={data.schedule ? humanCron(data.schedule) : data.reason}
      />
      <Cell
        label="Pass rate"
        value={
          run?.pass_rate != null ? `${Math.round(run.pass_rate * 100)}%` : "--"
        }
        note={
          run?.sessions_passed != null && run.sessions != null
            ? `${run.sessions_passed} of ${run.sessions} ${run.sessions === 1 ? "trajectory" : "trajectories"}`
            : "No trajectories evaluated"
        }
      />
      <Cell
        label="Open insights"
        value={String(data.open_findings)}
        note={data.worst_finding?.label ?? "Nothing outstanding"}
        to="/insights"
      />
      <Cell
        label="Last investigation"
        value={run ? formatSentenceCase(ago(run.finished_at)) : "Never"}
        note={
          run
            ? formatSentenceCase(
                `${run.trigger_type} investigation ${run.run_id}`,
              )
            : "No investigation has run"
        }
        {...(run
          ? { to: "/investigations/$runId", params: { runId: run.run_id } }
          : {})}
      />
    </section>
  );
}
