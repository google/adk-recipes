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

/** Component for rendering structured root-cause records in detail panels and chat tools. */

import { AlertTriangle, Loader2 } from "lucide-react";

import { DiffBlock } from "@/components/chat/tool-views";
import type { ProposedEdit, RootCause } from "@/lib/aqua-api";
import { mono, textStyle } from "@/lib/typography";
import { cn } from "@/lib/utils";

/** Tool name for root-cause recording operations. */
export const RECORD_ROOT_CAUSE_TOOL = "record_root_cause";

/** An edit rejected during source snapshot anchoring. */
export interface RejectedEdit {
  path: string;
  start_line: number;
  end_line: number;
  reason: string;
}

/** Response payload from the root-cause recording tool. */
export interface RecordRootCausePayload {
  recorded?: boolean;
  root_cause_id?: string;
  record?: RootCause | null;
  edits_recorded?: number;
  /** Edits successfully anchored before any subsequent failure. */
  anchored?: ProposedEdit[];
  rejected?: RejectedEdit[];
  warnings?: string[];
  error?: string;
}

const lineRange = (edit: Pick<ProposedEdit, "start_line" | "end_line">) =>
  `${edit.start_line}-${edit.end_line}`;

/** Compute a unique key for an edit based on file path and line range. */
const editKey = (
  edit: Pick<ProposedEdit, "path" | "start_line" | "end_line">,
) => `${edit.path}:${lineRange(edit)}`;

/** Find the most recent root-cause record by creation timestamp.
 *
 * @param records List of root-cause records to evaluate.
 * @returns The most recent record, or undefined if the list is empty.
 */
export function newestRootCause(
  records: readonly RootCause[],
): RootCause | undefined {
  return records.reduce<RootCause | undefined>(
    (newest, record) =>
      !newest || record.created_at > newest.created_at ? record : newest,
    undefined,
  );
}

/** Render a root-cause summary, code diffs, and validation warnings. */
export function RootCauseRecord({
  record,
  warnings = [],
  note,
}: {
  record: RootCause;
  warnings?: string[];
  /** Optional informational note displayed above the summary. */
  note?: string;
}) {
  const edits = record.edits ?? [];
  return (
    <section
      data-testid="root-cause-record"
      className="flex w-full max-w-full flex-col gap-2"
    >
      {note && <p className={textStyle.meta}>{note}</p>}
      <p className={cn(textStyle.meta, "text-foreground")}>{record.summary}</p>
      {edits.length === 0 ? (
        <p className={textStyle.meta}>No code changes proposed.</p>
      ) : (
        edits.map((edit) => <EditBlock key={editKey(edit)} edit={edit} />)
      )}
      <Warnings warnings={warnings} />
    </section>
  );
}

function EditBlock({ edit }: { edit: ProposedEdit }) {
  return (
    <div className="flex w-full max-w-full flex-col gap-1">
      <span className={cn(textStyle.meta, mono)}>
        {edit.path}:{lineRange(edit)}
      </span>
      {edit.rationale && <p className={textStyle.meta}>{edit.rationale}</p>}
      <DiffBlock oldString={edit.before ?? ""} newString={edit.after ?? ""} />
    </div>
  );
}

function Warnings({ warnings }: { warnings: string[] }) {
  if (warnings.length === 0) return null;
  return (
    <ul className="flex flex-col gap-1">
      {warnings.map((warning) => (
        <li
          key={warning}
          className={cn(
            textStyle.meta,
            "flex items-start gap-1.5 rounded border border-amber-500/30 bg-amber-500/10 px-2 py-1 text-amber-700 dark:text-amber-300",
          )}
        >
          <AlertTriangle className="mt-0.5 h-3 w-3 shrink-0" />
          {warning}
        </li>
      ))}
    </ul>
  );
}

/** Render root-cause tool invocation status, anchored edits, or rejection details.
 *
 * Renders from the result payload to display server-anchored diffs and any
 * rejected edits.
 */
export function RootCauseToolResult({
  result,
  hasResult,
}: {
  result: unknown;
  hasResult: boolean;
}) {
  if (!hasResult) {
    return (
      <div className={cn(textStyle.meta, "flex items-center gap-1.5")}>
        <Loader2 className="h-3 w-3 motion-safe:animate-spin" />
        recording the root cause…
      </div>
    );
  }
  const payload = (result ?? {}) as RecordRootCausePayload;
  if (payload.error) {
    return <Failure>{payload.error}</Failure>;
  }
  if (payload.recorded && payload.record) {
    return (
      <RootCauseRecord
        record={payload.record}
        warnings={payload.warnings ?? []}
      />
    );
  }
  return <RejectionReport payload={payload} />;
}

/** Display rejected edits and anchoring failures that prevented recording. */
function RejectionReport({ payload }: { payload: RecordRootCausePayload }) {
  const rejected = payload.rejected ?? [];
  const anchored = payload.anchored ?? [];
  return (
    <div
      data-testid="root-cause-rejected"
      className="flex w-full max-w-full flex-col gap-2"
    >
      <Failure>
        No root cause was recorded: {rejected.length || "no"} edit
        {rejected.length === 1 ? "" : "s"} could not be anchored to the source.
      </Failure>
      <ul className="flex flex-col gap-1">
        {rejected.map((edit) => (
          <li key={editKey(edit)} className="flex flex-col gap-0.5">
            <span className={cn(textStyle.meta, mono)}>
              {edit.path}:{lineRange(edit)}
            </span>
            <span className={textStyle.meta}>{edit.reason}</span>
          </li>
        ))}
      </ul>
      {anchored.length > 0 && (
        <p className={textStyle.meta}>
          {anchored.length} edit{anchored.length === 1 ? "" : "s"} anchored
          cleanly and can be resent.
        </p>
      )}
      <Warnings warnings={payload.warnings ?? []} />
    </div>
  );
}

function Failure({ children }: { children: React.ReactNode }) {
  return (
    <div
      className={cn(
        textStyle.meta,
        "rounded border border-rose-500/30 bg-rose-500/10 px-2 py-1",
        "text-rose-700 dark:text-rose-300",
      )}
    >
      {children}
    </div>
  );
}

/** Format a summary string of proposed edits for collapsed tool chips.
 *
 * @param args Tool call arguments containing proposed edits.
 * @returns Short description of edit count and affected files.
 */
export function rootCausePreview(args: Record<string, unknown> | null): string {
  const edits = Array.isArray(args?.edits)
    ? (args.edits as ProposedEdit[])
    : [];
  if (edits.length === 0) return "no edits";
  const paths = [...new Set(edits.map((edit) => edit?.path).filter(Boolean))];
  const where = paths.length === 1 ? paths[0] : `${paths.length} files`;
  return `${edits.length} edit${edits.length === 1 ? "" : "s"} · ${where}`;
}
