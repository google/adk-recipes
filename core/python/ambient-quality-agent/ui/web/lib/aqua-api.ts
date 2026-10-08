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

/** AQuA's own dashboard endpoints, served by `ambient_quality_ui/app.py`.
 *
 * Separate from `lib/horizon-*`: those talk to a harness AQuA does not run.
 * Field names are snake_case because they come straight from the agent's
 * Pydantic models; renaming them here would just add a place to drift.
 */

import { DASHBOARD_DAYS, dayWindow, windowStart } from "./day-buckets";

/** Carries the HTTP status so the retry policy can tell permanent from transient. */
export class HttpError extends Error {
  constructor(
    message: string,
    readonly status: number,
  ) {
    super(message);
    this.name = "HttpError";
  }
}

/** A proposed code replacement anchored against a source snapshot.
 *
 * `before` is populated server-side from the pinned source snapshot to reflect
 * the actual code at the specified lines.
 */
export interface ProposedEdit {
  path: string;
  /** 1-based line number (inclusive). */
  start_line: number;
  /** 1-based line number (inclusive). */
  end_line: number;
  before: string;
  after: string;
  rationale?: string;
}

/** A recorded root cause diagnosis and its proposed code edits. */
export interface RootCause {
  root_cause_id: string;
  insight_id: string;
  occurrence_id: string;
  /** Snapshot revision used to resolve edit line ranges. */
  agent_revision: string;
  summary: string;
  edits: ProposedEdit[];
  created_at: string;
  /** Total number of proposed edits, preserved even when edit bodies are omitted. */
  edit_count?: number;
}

export interface InsightView {
  insight_id: string;
  agent_name: string;
  label: string;
  tool_name: string;
  status: "NEW" | "RECURRING" | "RESOLVED";
  occurrence_count: number;
  trace_count: number;
  impact: number;
  last_run_id?: string | null;
  last_run_at: string | null;
  /** When the issue was first recorded, and when a sweep closed it. Both come
   *  off the `Insight` model the server's `InsightView` extends; `resolved_at`
   *  is null for an issue still open, and also for one resolved before the
   *  column existed — see `resolutionDate` in `lib/insight-trend`. */
  created_at?: string | null;
  updated_at?: string | null;
  resolved_at?: string | null;
  /** The recorded root cause, when there is one. Empty until someone
   *  asks AQuA to diagnose the issue — the sweep itself does not. */
  diagnosis: string;
  /** A sharper name for the defect, proposed by cluster verification. Empty
   * when it proposed none. `label` stays the stored recurrence key. */
  refined_label?: string;
  /** Where the defect originates: `agent_behavior` when the agent departed from
   * a specification it was given, `agent_definition` when the specification
   * itself is at fault. Empty when the pass did not run. */
  confidence: number;
  /** Whether a root cause has been recorded for this insight. */
  has_root_cause?: boolean;
}

/** Evaluates whether an insight is diagnosed via an autorater diagnosis or a
 * recorded root cause, providing a consistent predicate for counts and badges.
 *
 * @param insight - The insight to evaluate.
 * @returns True if the insight has a diagnosis or recorded root cause.
 */
export function isDiagnosed(insight: Partial<InsightView>): boolean {
  return Boolean(insight.diagnosis) || Boolean(insight.has_root_cause);
}

export interface InsightsPage {
  insights: InsightView[];
  /** How many issues match the filter, across every page. */
  total: number;
  /** Distinct conversations across every matching insight, not just this page. */
  conversations?: number;
  /** Pass back as `pageToken` for the next page; null on the last one. */
  next_page_token?: string | null;
  error?: string;
}

/** How the engine ranks the issues before it pages them. Mirrors
 *  `reader.InsightOrder`. */
export type InsightOrder = "recent" | "impact";

/** Rows on a page of the issue list.
 *
 * The client's number, not the engine's: it is how many rows the list draws,
 * and the list asks for exactly that many. It lives here so the boot prefetch
 * warms the page the list will ask for rather than a differently-sized one.
 */
export const INSIGHTS_PAGE_SIZE = 10;

/** One figure per `InvestigationCounters` field, summed over the runs in the
 * window, plus how many runs produced them.
 *
 * Not every field the agent declares is here — only the ones something
 * renders. Adding one is a type change and nothing else: `counter_totals` sums
 * every field of the model in one statement, so they are all already on the
 * wire. */
export interface Stats {
  investigations: number;
  traces_scanned: number;
  traces_ingested: number;
  /** Ingested, but on less telemetry than the agent produced. */
  traces_ingested_partial: number;
  /** Sampled and then dropped before evaluation. */
  traces_ingested_failed: number;
  traces_evaluated: number;
  traces_eval_passed: number;
  traces_eval_failed: number;
  traces_eval_errored: number;
  insights_created: number;
  insights_recurring: number;
}

const ZERO_STATS: Stats = {
  investigations: 0,
  traces_scanned: 0,
  traces_ingested: 0,
  traces_ingested_partial: 0,
  traces_ingested_failed: 0,
  traces_evaluated: 0,
  traces_eval_passed: 0,
  traces_eval_failed: 0,
  traces_eval_errored: 0,
  insights_created: 0,
  insights_recurring: 0,
};

/** An engine deployed before a counter existed omits it, and a deployment with
 * no runs at all answers with `{}`. Either way the arithmetic downstream turns
 * `undefined` into NaN% and paints a blank bar, so the zeros are filled once,
 * here, rather than guessed at by every consumer. */
export function withStatsDefaults(raw: Partial<Stats> | undefined): Stats {
  return { ...ZERO_STATS, ...(raw ?? {}) };
}

export interface Day {
  day: string;
  traces_evaluated: number;
  traces_eval_passed: number;
  traces_eval_failed: number;
  traces_eval_errored: number;
  /** Issues seen for the first time that day. `counters_by_day` sums every
   *  counter, so this has always been on the wire. */
  insights_created?: number;
  /** Sweeps that closed a telemetry window on this day. Zero days are absent
   *  from the response entirely, so this is what tells "swept and found
   *  nothing" from "never swept". */
  investigations?: number;
  /** Of those sweeps, how many recorded no counters at all — failed, skipped
   *  or still running. A day that is all-unmeasured looked at nothing, which
   *  is not the same as looking and finding none. */
  unmeasured?: number;
}

export interface Run {
  run_id: string;
  /** The chat that started this run, or null when the scheduler did. */
  context_id?: string | null;
  observed_agent_name: string;
  /** What launched the sweep: `manual` for a person or the CLI, otherwise the
   *  ambient trigger's own type (`scheduled`, `task_fire`, `update`). Empty on
   *  a record that predates the field. */
  trigger_type?: string | null;
  /** Traces the run was allowed to evaluate per metric, fixed from the config
   *  when it was submitted. `0` is the model default and a real answer — it
   *  bounds nothing — so it is rendered rather than treated as unset. */
  budget_per_metric?: number | null;
  created_at: string;
  finished_at: string | null;
  /** Lifecycle state: scheduled / pending / running / done / failed /
   *  skipped. What `lib/runstate` reads to decide whether a sweep is still in
   *  flight, and the only field that says a run has not finished.
   *
   *  `format_run` also passes an `rca_status` through from the stored summary.
   *  It is deliberately not declared here: nothing has written it since RCA was
   *  removed in #77, and `test_summarize_carries_metric_counts` pins it absent.
   *  Reading it as a run's status is a mistake this view already made once. */
  status: string;
  /** Start of the telemetry window the sweep covered. With `window_end` it is
   *  the period the run looked at, which is not when the run happened — and is
   *  what tells two sweeps of the same agent apart on the list. */
  window_start?: string | null;
  /** End of the telemetry window. Stands in as a start time for older records,
   *  written before `created_at` existed. */
  window_end?: string | null;
  /** When a `scheduled` run's delayed trigger is due to start it. The window
   *  is unset until then: it is worked out when the run starts. */
  due_at?: string | null;
  elapsed_seconds: number | null;
  /** `(case, metric)` outcomes — several per trace. Not what the funnel or the
   *  lists count; see `counters` for that. */
  metrics_passed: number;
  metrics_failed: number;
  metrics_errored: number;
  /** The run's own trace counters, always a full mapping — all-zero for a run
   *  that has produced none yet, and for one recorded before they existed.
   *  `format_run` emits it on list rows as well as on the detail record. */
  counters?: InvestigationCounters;
  error: string | null;
}

/** Ask for a sweep now.
 *
 * Returns the agent's own error rather than throwing on it: a refused start and
 * an unreachable agent read differently to whoever pressed the button, and only
 * the second is worth a retry.
 *
 * No timeout is imposed. With `sync_investigation` the POST blocks for minutes,
 * and in async mode it returns a pending run at once; in both cases the runs
 * list is what says whether a sweep is really going, so the caller re-reads it
 * either way.
 */
export async function startInvestigation(): Promise<{ error?: string }> {
  const r = await fetch("/api/investigations", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: "{}",
  });
  const body = (await r.json().catch(() => ({}))) as { error?: string };
  if (!r.ok && !body.error) {
    throw new HttpError(`start investigation -> ${r.status}`, r.status);
  }
  return body;
}

export interface InsightOccurrence {
  occurrence_id: string;
  run_id?: string;
  created_at: string;
  /** Kept on the wire for an engine that reports one; nothing renders it.
   * No phase in this pipeline measures a confidence. */
  confidence: number;
  diagnosis: string;
  refined_label?: string;
  label: string;
  item_count: number;
  trace_count: number;
  /** Cases a Cloud Trace lookup confirmed or cleared. Zero means the
   * diagnosis rests on what the model read, not on an independent check. */
  cases_checked: number;
  evidence_case_ids: string[];
  /** Cloud Trace console link per entry of `evidence_case_ids`. A case with no
   * entry has no trace to open: it was sampled before the trajectory store
   * existed, or came from the `big_query` source, whose analytics schema
   * carries no trace ids. */
  console_urls: Record<string, string>;
  rubrics?: Array<Record<string, unknown>>;
  /** Root-cause records associated with this occurrence. */
  root_causes?: RootCause[];
}

/** Evaluates whether an individual occurrence is diagnosed via its verification
 * diagnosis or attached root-cause records.
 *
 * @param occurrence - The occurrence to evaluate.
 * @returns True if the occurrence carries a diagnosis or root-cause records.
 */
export function isOccurrenceDiagnosed(
  occurrence: Partial<InsightOccurrence>,
): boolean {
  return (
    Boolean(occurrence.diagnosis) || (occurrence.root_causes?.length ?? 0) > 0
  );
}

export interface InsightDetail {
  insight: InsightView;
  occurrences: InsightOccurrence[];
  /** All root-cause records for this insight across all occurrences. */
  root_causes: RootCause[];
  /** Set when the engine held more occurrences than it sent. Nothing asks for
   *  the next page yet, so the pane reports the shortfall instead. */
  next_page_token?: string | null;
}

/** How long a dashboard read may hang before it is called a failure.
 *
 * Every read here goes through the agent, so a slow one is normal and a stuck
 * one is indistinguishable from it — without a bound, a wedged backend renders
 * as a spinner that never resolves, which is the one failure mode a reader
 * cannot act on. Ninety seconds is far longer than any of these reads takes
 * and short enough to still be an answer. */
export const READ_TIMEOUT_MS = 90_000;

/** A read that ran out of time, kept distinct from a read that failed.
 *
 * The two want different things from whoever is looking: a timeout is worth
 * retrying as-is, an unreachable agent is worth checking the deployment for.
 * Collapsing them into one "something went wrong" throws that away. */
export class ReadTimeoutError extends Error {
  constructor(path: string) {
    super(`${path} timed out after ${READ_TIMEOUT_MS}ms`);
    this.name = "ReadTimeoutError";
  }
}

async function getJson<T>(path: string): Promise<T> {
  // A controller and a timer rather than `AbortSignal.timeout`, so the abort
  // reason is ours to recognise and the behaviour does not depend on how new
  // the runtime is.
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), READ_TIMEOUT_MS);
  let r: Response;
  try {
    r = await fetch(path, { signal: controller.signal });
  } catch (err) {
    if (controller.signal.aborted) throw new ReadTimeoutError(path);
    throw err;
  } finally {
    clearTimeout(timer);
  }
  const body = (await r.json()) as T & { error?: string };
  if (!r.ok) throw new HttpError(`${path} -> ${r.status}`, r.status);
  if (body.error) throw new Error(body.error);
  return body;
}

/**
 * One page of the issue list — deliberately not scoped to the dashboard's
 * horizon.
 *
 * `pageSize` and `orderBy` are the caller's, so a view asks for exactly the
 * rows it draws in the order it draws them. Both are the engine's to apply:
 * ranking a page after it arrives ranks one window of the list, and the rows
 * that belonged at the top may be on a page nobody asked for.
 *
 * The endpoint's `windowStart` filters on `updated_at`, the *last sighting*,
 * which is a different question from the one the totals and the charts are
 * scoped by, and windowing on it breaks both callers. An open issue is retired
 * after `insights_auto_resolve_days` unseen, so a horizon of the same length
 * would drop the ones nearest that edge while they are still open. Worse, the
 * resolved series dates an auto-resolved row at its last sighting *plus* that
 * window — so those rows sit exactly one window older than the day they are
 * plotted on, and a matching `windowStart` would filter out precisely the
 * resolutions the chart is drawing.
 */
export const insightsQuery = (args?: {
  runId?: string;
  status?: string;
  /** Filter by diagnosis status. Omit to return all insights. */
  hasRootCause?: boolean;
  /** How many issues to fetch. Omit to take the engine's page size. */
  pageSize?: number;
  /** Omit for the engine's default order (last seen first). */
  orderBy?: InsightOrder;
  /** `next_page_token` from the previous page; omit for the first. */
  pageToken?: string;
  /** A UTC day, `YYYY-MM-DD`: only the insights found or resolved on it, with
   *  `status` choosing which of the two. Omit for every day. */
  day?: string;
}) => ({
  queryKey: [
    "aqua",
    "insights",
    args?.runId ?? null,
    args?.status ?? null,
    args?.hasRootCause ?? null,
    args?.pageSize ?? null,
    args?.orderBy ?? null,
    args?.pageToken ?? null,
    args?.day ?? null,
  ] as const,
  queryFn: async (): Promise<InsightsPage> => {
    const params = new URLSearchParams();
    if (args?.runId) params.set("runId", args.runId);
    if (args?.status) params.set("status", args.status);
    // Forward as a string so false is not dropped by falsy-filtering routes.
    if (args?.hasRootCause !== undefined) {
      params.set("hasRootCause", String(args.hasRootCause));
    }
    if (args?.pageSize) params.set("pageSize", String(args.pageSize));
    if (args?.orderBy) params.set("orderBy", args.orderBy);
    if (args?.pageToken) params.set("pageToken", args.pageToken);
    if (args?.day) params.set("day", args.day);
    const qs = params.toString();
    const page = await getJson<InsightsPage>(
      qs ? `/api/insights?${qs}` : "/api/insights",
    );
    return { ...page, insights: (page.insights ?? []).map(withDefaults) };
  },
  staleTime: 30_000,
});

export async function dismissInsight(
  insightId: string,
): Promise<{ dismissed: boolean }> {
  const r = await fetch(
    `/api/insights/${encodeURIComponent(insightId)}/dismiss`,
    {
      method: "POST",
    },
  );
  const body = (await r.json()) as { dismissed?: boolean; error?: string };
  if (!r.ok)
    throw new HttpError(`dismiss ${insightId} -> ${r.status}`, r.status);
  if (body.error) throw new Error(body.error);
  return { dismissed: Boolean(body.dismissed) };
}

export async function mergeInsights(args: {
  insightId: string;
  targetInsightId: string;
}): Promise<{ merged: boolean }> {
  const r = await fetch(
    `/api/insights/${encodeURIComponent(args.insightId)}/merge`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ target_insight_id: args.targetInsightId }),
    },
  );
  const body = (await r.json()) as { merged?: boolean; error?: string };
  if (!r.ok)
    throw new HttpError(`merge ${args.insightId} -> ${r.status}`, r.status);
  if (body.error) throw new Error(body.error);
  return { merged: Boolean(body.merged) };
}

/** Whether the ambient loop is turning. Mirrors `investigations/health.py`. */
export type AmbientVerdict =
  | "watching"
  | "overdue"
  | "never"
  | "failing"
  | "disabled";

export interface AmbientHealth {
  verdict: AmbientVerdict;
  reason: string;
  schedule: string | null;
  max_age_hours: number | null;
  last_scheduled_finish: string | null;
  last_run: {
    run_id: string;
    finished_at: string | null;
    status: string;
    trigger_type: string;
    /** Null when the run evaluated nothing, never 0. */
    sessions: number | null;
    /** Absolute count behind `pass_rate`; null when nothing was evaluated. */
    sessions_passed: number | null;
    pass_rate: number | null;
  } | null;
  open_findings: number;
  worst_finding: { insight_id: string; label: string } | null;
}

export const healthQuery = () => ({
  queryKey: ["aqua", "health"] as const,
  queryFn: () => getJson<AmbientHealth & { error?: string }>("/api/health"),
  // The homepage's first paint. Shorter than the lists: a stale "watching" is
  // the one wrong answer this panel must not give.
  staleTime: 15_000,
});

/**
 * `?windowStart=…`, the dashboard's horizon, for the reads that aggregate.
 *
 * Built in the query function rather than baked into the key: the key has to
 * stay stable for the cache to be worth anything, and an instant that moves
 * with the clock would make every render a fresh query.
 */
const scoped = (path: string): string =>
  `${path}?windowStart=${encodeURIComponent(windowStart(DASHBOARD_DAYS))}`;

/**
 * The funnel's figures, over the dashboard's horizon.
 *
 * Scoped, unlike the two lists below, because these are a *sum over a period*
 * and the period went unstated: with no window the server totals every run
 * ever recorded, so the funnel described the deployment's whole life while the
 * chart beside it described a fortnight, and neither said which.
 */
export const statsQuery = () => ({
  queryKey: ["aqua", "stats"] as const,
  queryFn: () =>
    getJson<{ stats: Partial<Stats> }>(scoped("/api/stats")).then((r) =>
      withStatsDefaults(r.stats),
    ),
  staleTime: 30_000,
});

/** The per-day rows the charts are drawn from, over the span they draw. */
export const dailyQuery = () => ({
  queryKey: ["aqua", "daily"] as const,
  queryFn: () =>
    getJson<{ days: Day[] }>(scoped("/api/daily")).then((r) => r.days ?? []),
  staleTime: 30_000,
});

/** The most runs one read of `/api/investigations` returns: the server's
 *  `DEFAULT_LIST_LIMIT`, which the endpoint does not let a caller raise. */
export const RUNS_LIST_LIMIT = 50;

/**
 * The most recent sweeps — deliberately *not* scoped to the horizon.
 *
 * The server caps this at its list limit and returns the newest page, so "the
 * last N sweeps" is already a complete answer, and every row carries its own
 * date. A window can only make it shorter: on a deployment sweeping daily it
 * would trade fifty rows of history for fourteen, and a log is read for depth.
 *
 * `day` is the exception, and a different question: the runs one column of the
 * per-day chart counts. So it windows on the field that chart buckets by, a
 * run's `window_end`, over that UTC day. The unscoped key stays a prefix of the
 * day's, so invalidating the recent runs refreshes every day read with them.
 */
export const runsQuery = (day?: string) => ({
  queryKey: (day
    ? ["aqua", "runs", day]
    : ["aqua", "runs"]) as readonly string[],
  queryFn: () =>
    getJson<{ runs: Run[] }>(
      day
        ? `/api/investigations?${new URLSearchParams(dayWindow(day))}`
        : "/api/investigations",
    ).then((r) => r.runs ?? []),
  staleTime: 30_000,
});

export interface GoalResponse {
  goal: string | null;
  available: boolean;
  reason?: string | null;
  /** Where the file lives, e.g. `gs://bucket/current/goal.md`. */
  uri?: string | null;
  /** Id of the active version: a hash of the goal text; null when no goal. */
  version?: string | null;
}

export interface GoalVersionRow {
  version: string;
  text: string;
  /** When the text was first saved. */
  created_at: string;
  /** When a save last made it the active goal; empty when none did. */
  last_activated_at: string;
  active: boolean;
}

export interface GoalVersionsResponse {
  /** Most recently active first. */
  versions: GoalVersionRow[];
  available: boolean;
  reason?: string | null;
}

/**
 * A summary of the newest complete source snapshot's manifest.
 *
 * `truncated_files` and `omitted_files` are the two ways publishing succeeds
 * and still leaves code that cannot be cited, so they are their own fields
 * rather than folded into `available`.
 */
export interface SourceResponse {
  available: boolean;
  /** Deployment revision the snapshot was published for. */
  revision?: string | null;
  /** How many revisions the bucket still holds, complete or not. */
  revision_count?: number | null;
  created_at?: string | null;
  agent_directory?: string | null;
  file_count?: number | null;
  total_bytes?: number | null;
  truncated_files?: number | null;
  omitted_files?: number | null;
  /** The manifest object, e.g. `gs://bucket/12/manifest.json`. */
  uri?: string | null;
  reason?: string | null;
}

export const sourceQuery = () => ({
  queryKey: ["aqua", "source"] as const,
  queryFn: () => getJson<SourceResponse>("/api/source"),
  staleTime: 5 * 60_000,
});

export interface EffectiveConfigResponse {
  config?: Record<string, unknown>;
  error?: string;
}

export const effectiveConfigQuery = () => ({
  queryKey: ["aqua", "effective-config"] as const,
  queryFn: () => getJson<EffectiveConfigResponse>("/api/config"),
  // The values only change on redeploy, and the read goes through the agent.
  staleTime: 5 * 60_000,
});

export interface MemoryRow {
  id: string;
  text: string;
  /** The chat session that recorded it. */
  source: string;
  /** ISO 8601, in UTC. */
  created_at: string;
}

export interface MemoriesResponse {
  memories: MemoryRow[];
  available: boolean;
  reason?: string | null;
  /** Where the file lives. A different bucket from the goal's. */
  uri?: string | null;
}

/**
 * One part of an event: text, a tool call, or a tool's reply.
 *
 * All three optional rather than a union, because that is how `genai` models a
 * part; re-encoding it here would be a second schema to hold level with the
 * one the archive stores.
 */
export interface ConversationPart {
  text?: string;
  function_call?: { id?: string | null; name?: string | null; args?: unknown };
  function_response?: {
    id?: string | null;
    name?: string | null;
    response?: unknown;
  };
}

export interface ConversationEvent {
  author?: string | null;
  content?: { role?: string | null; parts?: ConversationPart[] } | null;
  event_time?: string | null;
}

/** One ADK turn: the user's prompt and everything the agent did in reply. */
export interface ConversationTurn {
  turn_index?: number | null;
  turn_id?: string | null;
  events?: ConversationEvent[] | null;
}

/**
 * One archived conversation, as the payload table holds it.
 *
 * `status` is `ingested` / `partial` / `truncated` for a copy that exists and
 * `not_archived` for an id nobody stored. `turn_count` above `turns_returned`
 * means the manifest outlived its turns' partition; both at zero is a
 * conversation that really was empty. Three different answers, and a viewer
 * that draws them alike is lying about two of them.
 */
export interface CaseConversation {
  trajectory_id: string;
  status: string;
  turn_count: number;
  turns_returned: number;
  turns: ConversationTurn[];
  agents?: Record<string, unknown> | null;
  recorded_at?: string | null;
  /** The sweep's index row, when the case was reached through a run. */
  trajectory?: { console_url?: string | null; source?: string | null } | null;
}

/** A tool call reconciled with its reply, as the timeline hands it to a view. */
export interface ConversationToolCall {
  callId: string | null;
  turnIndex: number;
  toolName: string | null;
  args: unknown;
  response: unknown;
  isError: boolean;
}

export interface InvestigationEvent {
  run_id: string;
  created_at: string;
  text: string;
  source: string | null;
}

export interface InvestigationCounters {
  traces_scanned: number;
  traces_ingested: number;
  traces_ingested_partial: number;
  traces_ingested_failed: number;
  traces_evaluated: number;
  traces_eval_passed: number;
  traces_eval_failed: number;
  traces_eval_errored: number;
  rubrics_generated: number;
  findings_generated: number;
  rubrics_errored: number;
  rubrics_unclustered: number;
  clusters_created: number;
  clusters_verified: number;
  clusters_rejected: number;
  clusters_verify_skipped: number;
  clusters_verify_failed: number;
  insights_created: number;
  insights_recurring: number;
}

/** Per-metric trajectory outcome counts. */
export interface MetricOutcome {
  passed: number;
  failed: number;
  errored: number;
}

/** Per-metric execution metadata and skip reasons. */
export interface MetricDetail {
  /** Execution kind: local, remote, judged, predefined, or refused. */
  kind: string;
  /** Expected agent behavior contract. */
  expected?: string;
  /** Minimum score required to pass; null for a judge that reports scores only. */
  threshold?: number | null;
  /** Score summary of a judge with no threshold, which files no findings. */
  scores?: { count: number; mean: number; min: number; max: number };
  /** Imported model client modules, if any. */
  model_modules?: string[];
  /** Skip or refusal reason when the metric did not run. */
  not_run?: string;
}

export interface InvestigationSummary {
  metrics_by_name?: Record<string, MetricOutcome>;
  metrics_detail?: Record<string, MetricDetail>;
  [key: string]: unknown;
}

/** Custom overrides applied to an investigation run. */
export interface InvestigationCustomOverrides {
  /** SQL query that selects the conversations to review. */
  selector_sql: string;
  /** Free-text focus guiding the session reviewer. */
  session_review_focus: string;
}

export interface InvestigationRecord {
  run_id: string;
  /** The chat that started this run, or null when the scheduler did. */
  context_id?: string | null;
  status: string;
  observed_agent_name: string;
  trigger_type?: string;
  window_start?: string | null;
  window_end?: string | null;
  /** When a `scheduled` run's delayed trigger is due to start it. */
  due_at?: string | null;
  budget_per_metric?: number;
  /** Custom run overrides, or null for ambient runs. */
  custom_overrides?: InvestigationCustomOverrides | null;
  created_at: string;
  updated_at?: string;
  finished_at?: string | null;
  elapsed_seconds?: number | null;
  metrics_passed?: number | null;
  metrics_failed?: number | null;
  metrics_errored?: number | null;
  counters?: InvestigationCounters;
  events?: InvestigationEvent[];
  summary?: InvestigationSummary | null;
  error?: string | null;
  idempotency_key?: string | null;
}

export const investigationQuery = (runId: string) => ({
  queryKey: ["aqua", "investigation", runId] as const,
  queryFn: () =>
    getJson<{ run: InvestigationRecord }>(
      `/api/investigations/${encodeURIComponent(runId)}`,
    ),
  staleTime: 10_000,
});

export const caseConversationQuery = (runId: string, caseId: string) => ({
  queryKey: ["aqua", "case-conversation", runId, caseId] as const,
  queryFn: () =>
    getJson<{ case: CaseConversation }>(
      `/api/investigations/${encodeURIComponent(runId)}/cases/${encodeURIComponent(caseId)}`,
    ),
  staleTime: 60_000,
});

export const goalQuery = () => ({
  queryKey: ["aqua", "goal"] as const,
  queryFn: () => getJson<GoalResponse>("/api/goal"),
  staleTime: 30_000,
});

/** Keyed under `goalQuery`'s key, so invalidating the goal after a save
 *  refreshes the versions too. */
export const goalVersionsQuery = () => ({
  queryKey: ["aqua", "goal", "versions"] as const,
  queryFn: () => getJson<GoalVersionsResponse>("/api/goal/versions"),
  staleTime: 30_000,
});

export async function saveGoal(
  text: string,
): Promise<{ goal: string; version?: string }> {
  const r = await fetch("/api/goal", {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ goal: text, text }),
  });
  const body = (await r.json().catch(() => ({}))) as {
    goal?: string;
    version?: string;
    error?: string;
  };
  // The agent's reason first: "-> 400" alone does not say the goal was too long.
  if (body.error) throw new Error(body.error);
  if (!r.ok) throw new Error(`PUT /api/goal -> ${r.status}`);
  return { goal: body.goal ?? "", version: body.version };
}

export const memoriesQuery = () => ({
  queryKey: ["aqua", "memories"] as const,
  queryFn: () => getJson<MemoriesResponse>("/api/memories"),
  staleTime: 30_000,
});

export async function deleteMemory(id: string): Promise<{ deleted: boolean }> {
  const r = await fetch(`/api/memories/${encodeURIComponent(id)}`, {
    method: "DELETE",
  });
  const body = (await r.json()) as { deleted?: boolean; error?: string };
  if (!r.ok) throw new Error(`delete memory ${id} -> ${r.status}`);
  if (body.error) throw new Error(body.error);
  return { deleted: Boolean(body.deleted) };
}

export const insightQuery = (insightId: string) => ({
  queryKey: ["aqua", "insight", insightId] as const,
  queryFn: async (): Promise<InsightDetail> => {
    const detail = await getJson<InsightDetail>(
      `/api/insights/${encodeURIComponent(insightId)}`,
    );
    // The one query that skipped this, which is why the detail pane was the
    // only view that crashed rather than merely rendering blanks.
    return {
      insight: withDefaults(detail.insight ?? {}),
      occurrences: (detail.occurrences ?? []).map(withOccurrenceDefaults),
      root_causes: detail.root_causes ?? [],
      next_page_token: detail.next_page_token ?? null,
    };
  },
  staleTime: 30_000,
});

/** The finding fields are absent, not zero, on an insight nothing has
 * diagnosed — which is most of them. Filling them here means no consumer has
 * to decide what a missing value meant. */
function withDefaults(raw: Partial<InsightView>): InsightView {
  return {
    diagnosis: "",
    confidence: 0,
    impact: 0,
    has_root_cause: false,
    ...raw,
  } as InsightView;
}

/** The same, for an occurrence. `withDefaults` covers the insight only, and
 * these are different fields: the detail pane reads `evidence_case_ids.length`
 * unguarded, so an engine that omits it blanks the pane with a TypeError
 * rather than rendering an empty section. */
function withOccurrenceDefaults(
  raw: Partial<InsightOccurrence>,
): InsightOccurrence {
  return {
    diagnosis: "",
    confidence: 0,
    cases_checked: 0,
    evidence_case_ids: [],
    console_urls: {},
    rubrics: [],
    root_causes: [],
    ...raw,
  } as InsightOccurrence;
}

/** Whether nothing on the page has a root cause recorded.
 *
 * Named for what it measures rather than for what it used to be blamed on: it
 * was `isEngineSkew`, back when an undiagnosed page meant an engine with no
 * root-cause phase at all. There is one now — `record_root_cause`, which the
 * chat agent calls — so an all-undiagnosed page is the ordinary state of
 * issues nobody has asked about yet, not a capability gap.
 *
 * @param insights - Insights from the target run.
 * @returns True if the insights list is non-empty and entirely undiagnosed.
 */
export function noneDiagnosed(
  insights: readonly Partial<InsightView>[],
): boolean {
  return insights.length > 0 && !insights.some(isDiagnosed);
}
