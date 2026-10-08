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
import { AlertCircle } from "lucide-react";

import { GcsLink } from "@/components/ui/gcs-link";
import { Loading } from "@/components/ui/loading";
import { sourceQuery } from "@/lib/aqua-api";
import { mono, textStyle } from "@/lib/typography";
import { cn, formatBytes, formatRelativeTime } from "@/lib/utils";

/** An RFC 3339 instant as "2h ago", or null when absent or unparseable. */
function publishedAt(value: string | null | undefined): string | null {
  if (!value) return null;
  const when = new Date(value);
  return Number.isNaN(when.getTime()) ? null : formatRelativeTime(when);
}

/**
 * The observed agent's published source, summarized from its manifest.
 *
 * Third card because it is the third thing the investigation reads, and the
 * only one of the three with no UI to write it. Missing is the interesting
 * state: without a snapshot the chat answers "I have no access to the code" and
 * findings cite no source line, so an absent one is drawn as a warning rather
 * than hidden.
 *
 * A partial snapshot is drawn as the same warning. Publishing caps each file
 * and the snapshot as a whole, so it can report success having dropped exactly
 * the file a diagnosis needed; a card that said only "published" would read as
 * though nothing were wrong.
 */
export function SourceCard() {
  const { data, isLoading, error } = useQuery(sourceQuery());

  if (isLoading) {
    return (
      <section className="rounded-lg border bg-card p-4">
        <Loading label="Loading source…" />
      </section>
    );
  }

  const failure = error ? (error as Error).message : null;
  const missing = failure ?? (data && !data.available ? data.reason : null);
  const truncated = data?.truncated_files ?? 0;
  const omitted = data?.omitted_files ?? 0;
  const incomplete = !missing && (truncated > 0 || omitted > 0);
  const published = publishedAt(data?.created_at);
  const revisionCount = data?.revision_count ?? 0;

  return (
    <section
      className={
        missing || incomplete
          ? "flex flex-col gap-3 rounded-lg border border-amber-500/40 bg-card p-4"
          : "flex flex-col gap-3 rounded-lg border bg-card p-4"
      }
    >
      <div className="flex items-start justify-between gap-3">
        <div className="flex flex-col gap-1">
          <h2 className={textStyle.sectionTitle}>Source snapshot</h2>
          <p className={textStyle.meta}>
            The observed agent&apos;s repository, published at deploy time so
            the chat and the dig phase can cite its code.
          </p>
        </div>
        <GcsLink uri={data?.uri} />
      </div>

      {missing ? (
        <p
          className={cn(
            textStyle.meta,
            "flex items-start gap-1.5 text-amber-500",
          )}
        >
          <AlertCircle className="mt-0.5 h-3.5 w-3.5 shrink-0" />
          <span>{missing}</span>
        </p>
      ) : (
        <>
          <dl className="grid grid-cols-1 gap-x-6 gap-y-1.5 sm:grid-cols-2">
            <Row
              label="Revision"
              value={data?.revision || "—"}
              machine
              note={revisionCount > 1 ? `of ${revisionCount} kept` : undefined}
            />
            <Row label="Published" value={published ?? "—"} />
            <Row label="Files" value={String(data?.file_count ?? 0)} />
            <Row label="Size" value={formatBytes(data?.total_bytes ?? 0)} />
            {data?.agent_directory ? (
              <Row
                label="Agent directory"
                value={data.agent_directory}
                machine
              />
            ) : null}
          </dl>

          {incomplete && (
            <p
              className={cn(
                textStyle.meta,
                "flex items-start gap-1.5 text-amber-500",
              )}
            >
              <AlertCircle className="mt-0.5 h-3.5 w-3.5 shrink-0" />
              <span>
                {[
                  truncated > 0
                    ? `${truncated} ${truncated === 1 ? "file" : "files"} truncated`
                    : null,
                  omitted > 0
                    ? `${omitted} ${omitted === 1 ? "file" : "files"} omitted`
                    : null,
                ]
                  .filter(Boolean)
                  .join(" and ")}{" "}
                at the publishing size cap, so no diagnosis can cite a line of
                those.
              </span>
            </p>
          )}
        </>
      )}
    </section>
  );
}

/** One `<dl>` row, matching `effective-config-card.tsx`. */
function Row({
  label,
  value,
  note,
  machine = false,
}: {
  label: string;
  value: string;
  note?: string;
  /** The value is an identifier or a path, set in mono. */
  machine?: boolean;
}) {
  return (
    <div className="flex items-baseline justify-between gap-3 border-b border-border/40 py-1">
      <dt className={cn(textStyle.label, "shrink-0")}>{label}</dt>
      <dd
        className={cn(textStyle.meta, "truncate text-right text-foreground")}
        title={value}
      >
        <span className={machine ? mono : undefined}>{value}</span>
        {note && <span className="ml-1.5 text-muted-foreground">{note}</span>}
      </dd>
    </div>
  );
}
