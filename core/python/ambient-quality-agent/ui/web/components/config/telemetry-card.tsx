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

import { useEffect, useId, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  AlertCircle,
  AlertTriangle,
  ArrowRight,
  Check,
  Loader2,
  Minus,
} from "lucide-react";
import {
  checkTelemetry,
  effectiveConfigQuery,
  recordGrants,
  saveTelemetry,
  type EffectiveConfigResponse,
  type GrantCommand,
  type SettingChange,
  type TelemetryCheck,
  type TelemetryTable,
} from "@/lib/aqua-api";
import { configLabel } from "@/lib/config-fields";
import { mono, textStyle } from "@/lib/typography";
import { cn } from "@/lib/utils";
import { CopyButton } from "@/components/chat/copy-button";
import { Button } from "@/components/ui/button";
import { FailureNote } from "@/components/ui/failure-note";
import { HelpPopover } from "@/components/ui/help-popover";
import { Input } from "@/components/ui/input";
import { Loading } from "@/components/ui/loading";
import { useAgentKey } from "@/lib/agent-selection";

const TITLE = "Telemetry table";

/** Seconds to wait before each re-check while a denied read has a grant
 *  command, matching `aqua attach` while a new IAM binding takes effect. */
const RECHECK_DELAYS_SECONDS = [15, 45, 60, 60];

/** The sources whose ingestion reads message content from a bucket. */
const PAYLOAD_SOURCES = new Set(["cloud_logging", "cloud_ops"]);

type Tone = "success" | "warning" | "error" | "muted";

/** Display word and tone for each check status. */
const STATUSES: Record<string, { tone: Tone; word: string }> = {
  passed: { tone: "success", word: "ok" },
  empty: { tone: "warning", word: "empty" },
  denied: { tone: "warning", word: "denied" },
  missing: { tone: "error", word: "missing" },
  mismatch: { tone: "error", word: "wrong format" },
  error: { tone: "error", word: "failed" },
  skipped: { tone: "muted", word: "skipped" },
};

const TONE_ICONS = {
  success: Check,
  warning: AlertTriangle,
  error: AlertCircle,
  muted: Minus,
};

const TONE_TEXT: Record<Tone, string> = {
  success: "text-emerald-500",
  warning: "text-amber-500",
  error: "text-destructive",
  muted: "text-muted-foreground",
};

const TONE_BOXES: Record<Exclude<Tone, "muted">, string> = {
  success: "border-emerald-500/30 bg-emerald-500/5",
  warning: "border-amber-500/30 bg-amber-500/5",
  error: "border-destructive/30 bg-destructive/5",
};

/** Verdict message when a table is refused, keyed by the refusing read's
 *  status. The read's own `detail` in the list below has the specifics. */
const REFUSALS: Record<string, string> = {
  missing: "The table does not exist. Check the dataset and table names.",
  mismatch: "The table's columns do not match any source AQuA reads.",
  error: "AQuA could not read the table.",
};

/** Hint shown when Save is disabled because the table is unchecked or already
 *  saved. */
const SAVE_HINTS = {
  unchecked: "Check the table to save.",
  unchanged: "This table is already saved.",
};

/** Display name for each check. */
const CHECK_NAMES: Record<string, string> = {
  table: "Table",
  schema: "Columns",
  rows: "Sessions",
  payload: "Message content",
  log_view: "Log view",
};

const FIELD_LABELS: Record<string, string> = {
  telemetry_dataset: "Dataset",
  telemetry_table: "Table",
  telemetry_location: "Location",
  telemetry_ingestion_source: "Source",
  telemetry_payload_bucket: "Payload bucket",
};

/** A checked table and its check result. */
interface CheckedTable {
  table: TelemetryTable;
  result: TelemetryCheck;
}

interface CheckRequest {
  table: TelemetryTable;
  /** Previous check result for the same table. */
  previous?: TelemetryCheck;
  /** Whether this is an automatic re-check while a grant takes effect. */
  auto?: boolean;
}

/** Renders the configuration card that shows, checks, and updates the
 *  selected agent's BigQuery telemetry table. Before saving a table, a check
 *  tries every read an investigation makes as AQuA's service account and fills
 *  in the location and source from the table. */
export function TelemetryCard() {
  const selectedKey = useAgentKey();
  const { data, isLoading, error } = useQuery(
    effectiveConfigQuery(selectedKey),
  );
  const queryClient = useQueryClient();
  const [editing, setEditing] = useState(false);
  const [datasetDraft, setDatasetDraft] = useState<string | null>(null);
  const [tableDraft, setTableDraft] = useState<string | null>(null);
  const [bucketDraft, setBucketDraft] = useState("");
  const [checked, setChecked] = useState<CheckedTable | null>(null);
  // Re-checks since the user's last check or the last grant found in effect.
  const [rechecks, setRechecks] = useState(0);
  const [savedNotice, setSavedNotice] = useState(false);
  const datasetId = useId();
  const tableId = useId();
  const formId = useId();

  const closeEditor = () => {
    setEditing(false);
    setDatasetDraft(null);
    setTableDraft(null);
    setBucketDraft("");
  };

  const record = useMutation({
    mutationFn: (granted: { agentKey: string; grants: GrantCommand[] }) =>
      recordGrants(granted.agentKey, granted.grants),
  });

  const check = useMutation({
    mutationFn: ({ table }: CheckRequest) => checkTelemetry(table),
    onSuccess: (result, { table, previous, auto }) => {
      setChecked({ table, result });
      const granted = previous ? findGranted(previous, result) : [];
      if (granted.length) {
        record.mutate({ agentKey: table.agentKey, grants: granted });
      }
      // A grant taking effect starts the waits again, as each round of
      // grants does in `aqua attach`.
      if (auto) setRechecks((n) => (granted.length ? 0 : n + 1));
    },
    onError: (_error, { auto }) => {
      if (auto) setRechecks((n) => n + 1);
    },
  });

  const save = useMutation({
    mutationFn: ({ table, result }: CheckedTable) =>
      saveTelemetry(table, result.filled),
    onSuccess: (reply, saved) => {
      // Update the cache directly from the save reply: another instance of
      // the agent can serve the old attachment for a while.
      queryClient.setQueryData<EffectiveConfigResponse>(
        effectiveConfigQuery(selectedKey).queryKey,
        (old) =>
          old?.config
            ? { ...old, config: applyChanges(old.config, reply.changes) }
            : old,
      );
      // Once saved, the table has no pending changes, and an empty bucket
      // input keeps the saved bucket. Pending grants keep re-checking.
      setChecked((c) =>
        c && isSameTable(c.table, saved.table)
          ? {
              table: { ...c.table, payloadBucket: "" },
              result: { ...c.result, changes: [] },
            }
          : c,
      );
      closeEditor();
      setSavedNotice(true);
      setTimeout(() => setSavedNotice(false), 3000);
    },
  });

  const config = data?.config ?? {};
  const agentKey = readString(config, "agent_key");
  const storedDataset = readString(config, "telemetry_dataset");
  const storedTable = readString(config, "telemetry_table");
  const draft: TelemetryTable = {
    agentKey,
    dataset: (datasetDraft ?? storedDataset).trim(),
    table: (tableDraft ?? storedTable).trim(),
    payloadBucket: bucketDraft.trim(),
  };
  // Only use a check result that matches the current inputs; editing any
  // input requires a new check before saving.
  const current = checked && isSameTable(checked.table, draft) ? checked : null;
  const recheckDelay =
    current?.result.grants.length &&
    !check.isPending &&
    rechecks < RECHECK_DELAYS_SECONDS.length
      ? RECHECK_DELAYS_SECONDS[rechecks]
      : undefined;
  const runCheck = check.mutate;

  useEffect(() => {
    if (!current || recheckDelay === undefined) return;
    const timer = setTimeout(
      () =>
        runCheck({
          table: current.table,
          previous: current.result,
          auto: true,
        }),
      recheckDelay * 1000,
    );
    return () => clearTimeout(timer);
  }, [current, recheckDelay, runCheck]);

  if (isLoading) {
    return (
      <section className="rounded-lg border bg-card p-4">
        <Loading label="Loading the telemetry table…" />
      </section>
    );
  }

  if (error) {
    return (
      <section className="rounded-lg border bg-card p-4">
        <FailureNote lead="Could not load the telemetry table." error={error} />
      </section>
    );
  }

  if (!agentKey) {
    return (
      <section className="flex flex-col gap-2 rounded-lg border border-amber-500/40 bg-card p-4">
        <h2 className={textStyle.sectionTitle}>{TITLE}</h2>
        <p
          className={cn(
            textStyle.meta,
            "flex items-center gap-1.5 text-amber-500",
          )}
        >
          <AlertCircle className="h-4 w-4 shrink-0" />
          No agent is attached. Attach one with{" "}
          <span className={mono}>agents-cli aqua attach</span> to set its table.
        </p>
      </section>
    );
  }

  const agentName = readString(config, "observed_agent_name");
  const project =
    readString(config, "observed_project_id") ||
    readString(config, "project_id");
  const storedSource = readString(config, "telemetry_ingestion_source");
  // Use the last check's source even after inputs are edited, so the payload
  // bucket input does not disappear while the user is editing it.
  const source =
    checked?.result.filled.find((s) => s.field === "telemetry_ingestion_source")
      ?.value ?? storedSource;
  const result = current?.result;
  const saveable = Boolean(
    result && !result.refusal && result.changes.length > 0,
  );
  const account = result?.service_account || "AQuA's service account";
  // No hint when the table is refused: the verdict already explains why.
  const saveHint = !result
    ? SAVE_HINTS.unchecked
    : !result.refusal && result.changes.length === 0
      ? SAVE_HINTS.unchanged
      : "";

  const checkDraft = () => {
    setRechecks(0);
    runCheck({ table: draft, previous: current?.result });
  };

  const cancel = () => {
    closeEditor();
    check.reset();
    save.reset();
  };

  return (
    <section className="flex flex-col gap-4 rounded-lg border bg-card p-4">
      <div className="flex flex-col gap-1">
        <div className="flex items-center gap-1">
          <h2 className={textStyle.sectionTitle}>{TITLE}</h2>
          <HelpPopover topic="the telemetry table">
            A check uses AQuA&apos;s service account to test each read an
            investigation makes. It also detects the table&apos;s location and
            source. Nothing changes until you save.
          </HelpPopover>
        </div>
        <p className={textStyle.meta}>
          AQuA reads <span className={mono}>{agentName}</span>&apos;s
          conversations from this BigQuery table.
        </p>
      </div>

      {editing ? (
        <form
          id={formId}
          className="flex flex-col gap-3"
          onSubmit={(e) => {
            e.preventDefault();
            if (draft.dataset && draft.table && !check.isPending) checkDraft();
          }}
        >
          <div className="flex flex-col gap-1">
            <label htmlFor={datasetId} className={textStyle.label}>
              Dataset
            </label>
            <Input
              id={datasetId}
              value={datasetDraft ?? storedDataset}
              onChange={(e) => setDatasetDraft(e.target.value)}
              spellCheck={false}
              autoComplete="off"
              className={mono}
            />
          </div>
          <div className="flex flex-col gap-1">
            <label htmlFor={tableId} className={textStyle.label}>
              Table
            </label>
            <Input
              id={tableId}
              value={tableDraft ?? storedTable}
              onChange={(e) => setTableDraft(e.target.value)}
              spellCheck={false}
              autoComplete="off"
              className={mono}
            />
          </div>
          <p className={textStyle.meta}>
            Must be in <span className={mono}>{project}</span>, the agent&apos;s
            project; checking detects the location and source.
          </p>

          {(PAYLOAD_SOURCES.has(source) || bucketDraft) && (
            <details className="flex flex-col gap-1">
              <summary
                className={cn(textStyle.label, "cursor-pointer select-none")}
              >
                Payload bucket (optional)
              </summary>
              <div className="mt-2 flex flex-col gap-1">
                <Input
                  aria-label="Payload bucket"
                  value={bucketDraft}
                  onChange={(e) => setBucketDraft(e.target.value)}
                  spellCheck={false}
                  autoComplete="off"
                  className={mono}
                />
                <p className={textStyle.meta}>
                  Enter the agent&apos;s message-content bucket to check access
                  before sessions exist, or leave empty to keep the stored
                  bucket.
                </p>
              </div>
            </details>
          )}
        </form>
      ) : (
        <dl className="grid grid-cols-1 gap-x-6 sm:grid-cols-2">
          <Row label="Project" value={project} wide />
          <Row label="Dataset" value={storedDataset} wide />
          <Row label="Table" value={storedTable} wide />
          <Row
            label="Location"
            value={readString(config, "telemetry_location")}
          />
          <Row label="Source" value={storedSource} />
        </dl>
      )}

      {current && result && (
        <div className="flex flex-col gap-4 border-t pt-4">
          <Verdict result={result} agentName={agentName} />

          {result.grants.length > 0 && (
            <div className="flex flex-col gap-2">
              <p className={textStyle.label}>Grant access</p>
              <p className={textStyle.meta}>
                Run with your own credentials (for example, in Cloud Shell):
              </p>
              {result.grants.map((g) => (
                <div
                  key={buildGrantKey(g)}
                  className="relative rounded-md border bg-muted/40"
                >
                  <pre
                    className={cn(
                      textStyle.code,
                      "whitespace-pre-wrap break-all p-2 pr-11",
                    )}
                  >
                    {g.command}
                  </pre>
                  <CopyButton
                    getText={() => g.command}
                    label="Copy the command"
                    className="absolute right-1.5 top-1.5"
                  />
                </div>
              ))}
              <p className={cn(textStyle.meta, "flex items-center gap-1.5")}>
                {check.isPending ? (
                  <>
                    <Loader2
                      className="h-3.5 w-3.5 shrink-0 motion-safe:animate-spin"
                      aria-hidden="true"
                    />
                    Checking again…
                  </>
                ) : recheckDelay !== undefined ? (
                  `Checking again in ${recheckDelay} s. A new grant can take a minute or more to take effect.`
                ) : (
                  "Still denied. A new grant can take several minutes to take effect; check again later."
                )}
              </p>
            </div>
          )}

          {saveable && (
            <div className="flex flex-col gap-1">
              <p className={textStyle.label}>Changes on save</p>
              <dl className="flex flex-col">
                {result.changes.map((change) => (
                  <ChangeRow
                    key={change.field}
                    change={change}
                    reason={
                      result.filled.find((s) => s.field === change.field)
                        ?.reason
                    }
                  />
                ))}
              </dl>
            </div>
          )}

          {/* Collapse when every read passed; the verdict already says so. */}
          <details
            open={result.checks.some((c) => c.status !== "passed")}
            className="flex flex-col gap-2"
          >
            <summary
              className={cn(
                textStyle.label,
                "cursor-pointer truncate select-none",
              )}
              title={account}
            >
              Reads as {account}
            </summary>
            <ul className="mt-2 flex flex-col gap-2">
              {result.checks.map((c) => (
                <ReadRow
                  key={c.name}
                  name={c.name}
                  status={c.status}
                  detail={c.detail}
                  // Only show a target that differs from the checked table.
                  target={
                    c.target === `${project}.${draft.dataset}.${draft.table}`
                      ? ""
                      : c.target
                  }
                />
              ))}
            </ul>
          </details>

          {result.ungranted.length > 0 && (
            <p className={cn(textStyle.meta, "break-words")}>
              No grant command for{" "}
              <span className={mono}>{result.ungranted.join(", ")}</span>.
              Manually grant {account} read access.
            </p>
          )}

          {record.isSuccess && (
            <p className={textStyle.meta}>
              Recorded on the attachment so detaching the agent revokes access:{" "}
              <span className={mono}>
                {record.variables.grants.map((g) => g.resource).join(", ")}
              </span>
              .
            </p>
          )}
          {record.isError && (
            <p className={cn(textStyle.meta, "text-destructive")}>
              Could not record the grants on the attachment:{" "}
              {(record.error as Error).message}
            </p>
          )}
        </div>
      )}

      <div className="flex flex-wrap items-center justify-end gap-2">
        {editing ? (
          <>
            {saveHint && <span className={textStyle.meta}>{saveHint}</span>}
            {check.isError && (
              <span className={cn(textStyle.meta, "text-destructive")}>
                {(check.error as Error).message}
              </span>
            )}
            {save.isError && (
              <span className={cn(textStyle.meta, "text-destructive")}>
                {(save.error as Error).message}
              </span>
            )}
            <Button type="button" size="sm" variant="ghost" onClick={cancel}>
              Cancel
            </Button>
            <Button
              type="submit"
              form={formId}
              size="sm"
              variant={current ? "outline" : "default"}
              disabled={!draft.dataset || !draft.table || check.isPending}
            >
              {check.isPending
                ? "Checking…"
                : current
                  ? "Check again"
                  : "Check table"}
            </Button>
            <Button
              type="button"
              size="sm"
              disabled={!saveable || check.isPending || save.isPending}
              onClick={() => current && save.mutate(current)}
            >
              {save.isPending ? "Saving…" : "Save table"}
            </Button>
          </>
        ) : (
          <>
            {savedNotice && (
              <span
                className={cn(
                  textStyle.meta,
                  "flex items-center gap-1 text-emerald-500",
                )}
              >
                <Check className="h-3.5 w-3.5" /> Saved
              </span>
            )}
            {check.isError && (
              <span className={cn(textStyle.meta, "text-destructive")}>
                {(check.error as Error).message}
              </span>
            )}
            <Button
              type="button"
              size="sm"
              variant="outline"
              disabled={check.isPending}
              onClick={checkDraft}
            >
              {check.isPending
                ? "Checking…"
                : current
                  ? "Check again"
                  : "Check access"}
            </Button>
            <Button
              type="button"
              size="sm"
              variant="outline"
              onClick={() => setEditing(true)}
            >
              Change table
            </Button>
          </>
        )}
      </div>
    </section>
  );
}

/** Renders the summary banner for a check result and the next step to take. */
function Verdict({
  result,
  agentName,
}: {
  result: TelemetryCheck;
  agentName: string;
}) {
  const { tone, title, body } = judgeCheck(result, agentName);
  const Icon = TONE_ICONS[tone];
  return (
    <div
      role={tone === "error" ? "alert" : "status"}
      className={cn(
        "flex items-start gap-2 rounded-md border p-3",
        TONE_BOXES[tone],
      )}
    >
      <Icon
        className={cn("mt-0.5 h-4 w-4 shrink-0", TONE_TEXT[tone])}
        aria-hidden="true"
      />
      <div className="flex min-w-0 flex-col gap-0.5">
        <p className={textStyle.itemTitle}>{title}</p>
        <p className={cn(textStyle.meta, "[overflow-wrap:anywhere]")}>{body}</p>
      </div>
    </div>
  );
}

function judgeCheck(
  result: TelemetryCheck,
  agentName: string,
): { tone: Exclude<Tone, "muted">; title: string; body: string } {
  if (result.refusal) {
    const failed = result.checks.find(
      (c) =>
        c.status === "missing" ||
        c.status === "mismatch" ||
        (c.status === "error" && (c.name === "table" || c.name === "rows")),
    );
    return {
      tone: "error",
      title: "AQuA can't use this table",
      body: (failed && REFUSALS[failed.status]) ?? result.refusal,
    };
  }
  const hasStatus = (status: string) =>
    result.checks.some((c) => c.status === status);
  if (hasStatus("denied")) {
    return {
      tone: "warning",
      title: "AQuA needs read access",
      body: result.changes.length
        ? "Grant access below. You can save now and grant access later."
        : "Grant access below.",
    };
  }
  if (hasStatus("error")) {
    return {
      tone: "warning",
      title: "Some reads failed",
      body: "Investigations will miss data from failed reads. See the reads below for details.",
    };
  }
  if (hasStatus("empty")) {
    return {
      tone: "warning",
      title: `No sessions for ${agentName} yet`,
      body: "You can save this table now. Investigations will find sessions once the agent has any.",
    };
  }
  const sessions = result.checks.find((c) => c.name === "rows")?.detail;
  return {
    tone: "success",
    title: "AQuA can read this table",
    body: [
      sessions,
      result.changes.length
        ? "Save to use this table."
        : "AQuA already reads this table.",
    ]
      .filter(Boolean)
      .join(" "),
  };
}

/** Renders one stored setting as a `<dl>` row. When `wide` is true, the row
 *  spans both columns for long values. */
function Row({
  label,
  value,
  wide = false,
}: {
  label: string;
  value: string;
  wide?: boolean;
}) {
  return (
    <div
      className={cn(
        "flex items-baseline justify-between gap-3 border-b border-border/40 py-1",
        wide && "sm:col-span-2",
      )}
    >
      <dt className={cn(textStyle.label, "shrink-0")}>{label}</dt>
      <dd
        className={cn(textStyle.meta, mono, "truncate text-foreground")}
        title={value}
      >
        {value || "—"}
      </dd>
    </div>
  );
}

/** Renders one field that a save will change, including `reason` when the
 *  value was filled in from the table. */
function ChangeRow({
  change,
  reason,
}: {
  change: SettingChange;
  reason?: string;
}) {
  return (
    <div className="flex items-baseline gap-3 border-b border-border/40 py-1 last:border-b-0">
      <dt className={cn(textStyle.label, "w-20 shrink-0")}>
        {FIELD_LABELS[change.field] ?? configLabel(change.field)}
      </dt>
      <dd
        className={cn(
          textStyle.meta,
          "flex min-w-0 flex-1 items-center gap-1.5",
        )}
      >
        <ChangeValue value={change.before} />
        <ArrowRight className="h-3 w-3 shrink-0" aria-hidden="true" />
        <span className="sr-only">to</span>
        <ChangeValue value={change.after} className="text-foreground" />
        {reason && (
          <span className="shrink-0" title={reason}>
            from the table
          </span>
        )}
      </dd>
    </div>
  );
}

function ChangeValue({
  value,
  className,
}: {
  value: unknown;
  className?: string;
}) {
  const text =
    value === null || value === undefined || value === "" ? "—" : String(value);
  return (
    <span className={cn(mono, "min-w-0 truncate", className)} title={text}>
      {text}
    </span>
  );
}

/** Renders one read from a check: its status, detail, and target when the
 *  target is not the table itself. */
function ReadRow({
  name,
  status,
  detail,
  target,
}: {
  name: string;
  status: string;
  detail: string;
  target: string;
}) {
  const { tone, word } = STATUSES[status] ?? {
    tone: "muted" as const,
    word: status,
  };
  const Icon = TONE_ICONS[tone];
  return (
    <li className="flex gap-2">
      <Icon
        className={cn("mt-0.5 h-3.5 w-3.5 shrink-0", TONE_TEXT[tone])}
        aria-hidden="true"
      />
      <div className="flex min-w-0 flex-1 flex-col gap-0.5">
        <p className={textStyle.body}>
          {CHECK_NAMES[name] ?? name}
          {status === "passed" ? (
            <span className="sr-only"> {word}</span>
          ) : (
            <span className={cn(textStyle.label, "ml-2", TONE_TEXT[tone])}>
              {word}
            </span>
          )}
        </p>
        {detail && (
          <p className={cn(textStyle.meta, "[overflow-wrap:anywhere]")}>
            {detail}
          </p>
        )}
        {target && (
          <p className={cn(textStyle.meta, mono, "truncate")} title={target}>
            {target}
          </p>
        )}
      </div>
    </li>
  );
}

/** Returns `config[field]` if it is a string, or `""` otherwise. */
function readString(config: Record<string, unknown>, field: string): string {
  const value = config[field];
  return typeof value === "string" ? value : "";
}

/** Returns a copy of `config` with its fields updated from `changes`. */
function applyChanges(
  config: Record<string, unknown>,
  changes: SettingChange[],
): Record<string, unknown> {
  const next = { ...config };
  for (const change of changes) {
    // Update only the fields that the configuration has. The payload
    // bucket is stored on the attachment, so /api/config does not return it.
    if (change.field in next) next[change.field] = change.after;
  }
  return next;
}

function isSameTable(a: TelemetryTable, b: TelemetryTable): boolean {
  return (
    a.agentKey === b.agentKey &&
    a.dataset === b.dataset &&
    a.table === b.table &&
    a.payloadBucket === b.payloadBucket
  );
}

/** Returns the resource-and-role key for `grant`, matching
 *  `grant_commands.Grant.key`. */
function buildGrantKey(grant: GrantCommand): string {
  return `${grant.resource} ${grant.role}`;
}

/** Returns the grants from `previous` that are in effect in `result`:
 *  omitted from `result.grants`, and every check they cover passed or empty. */
function findGranted(
  previous: TelemetryCheck,
  result: TelemetryCheck,
): GrantCommand[] {
  const allowed = new Set(
    result.checks
      .filter((c) => c.status === "passed" || c.status === "empty")
      .map((c) => c.name),
  );
  const needed = new Set(result.grants.map(buildGrantKey));
  return previous.grants.filter(
    (g) =>
      !needed.has(buildGrantKey(g)) && g.checks.every((n) => allowed.has(n)),
  );
}
