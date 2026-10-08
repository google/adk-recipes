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

import { createFileRoute } from "@tanstack/react-router";
import { useQuery } from "@tanstack/react-query";
import { AlertCircle, ExternalLink } from "lucide-react";
import { caseConversationQuery, type CaseConversation } from "@/lib/aqua-api";
import { CaseBackLink } from "@/components/investigations/case-back-link";
import { fromConversation } from "@/lib/trajectory/fromConversation";
import { Trajectory } from "@/components/trajectory/trajectory";
import { ConversationView } from "@/components/conversation/conversation-view";
import { PageShell } from "@/components/nav/page-shell";
import { Loading } from "@/components/ui/loading";
import { PageHeader } from "@/components/nav/page-header";
import { link, mono, textStyle } from "@/lib/typography";
import { cn } from "@/lib/utils";

/** Why a conversation is not all here, or null when it is.
 *
 * Three states the archive distinguishes and a timeline cannot: nothing was
 * ever stored, the turns aged past their partition while the manifest stayed,
 * and the copy was made from lossy telemetry. Drawn as a note above the replay,
 * because an empty timeline otherwise reads as "the agent did nothing". */
export function archiveNote(archived: CaseConversation): string | null {
  if (archived.status === "not_archived") {
    return (
      "This trajectory was never archived. It was sampled before the " +
      "trajectory store existed, or its investigation could not assemble it."
    );
  }
  if (archived.turn_count > archived.turns_returned) {
    if (archived.turns_returned === 0) {
      return (
        `The archived copy has expired: ${archived.turn_count} ${archived.turn_count === 1 ? "turn was" : "turns were"} ` +
        "recorded and none are still stored. The trajectory is gone; this " +
        "page is what is left of it."
      );
    }
    return (
      `Partial archive: ${archived.turns_returned} of ${archived.turn_count} recorded ` +
      `${archived.turn_count === 1 ? "turn is" : "turns are"} still stored.`
    );
  }
  if (archived.status === "partial") {
    return "Assembled from incomplete telemetry, so some of it is missing.";
  }
  if (archived.status === "truncated") {
    return "A turn exceeded the archive's size cap and was left out.";
  }
  return null;
}

export const Route = createFileRoute("/investigations/$runId/cases/$caseId")({
  component: function CaseConversationRoute() {
    const { runId, caseId } = Route.useParams();
    const { data, isLoading, isError, error } = useQuery(
      caseConversationQuery(runId, caseId),
    );

    return (
      <PageShell>
        <div className="flex flex-col gap-5">
          <CaseBackLink runId={runId} />

          <PageHeader
            title={
              <>
                Trajectory{" "}
                <span className={cn(mono, "break-all")}>{caseId}</span>
              </>
            }
          >
            {data?.case?.trajectory?.console_url && (
              <a
                href={data.case.trajectory.console_url}
                target="_blank"
                rel="noreferrer"
                className={cn(
                  textStyle.meta,
                  link.standalone,
                  "inline-flex w-fit items-center gap-1 hover:text-foreground",
                )}
              >
                <ExternalLink className="h-3 w-3" /> Open in Cloud Trace
              </a>
            )}
          </PageHeader>

          {isLoading && <Loading className="p-4" label="Loading trajectory…" />}
          {isError && (
            <p className={cn(textStyle.meta, "p-4 text-destructive")}>
              {(error as Error).message}
            </p>
          )}
          {data?.case && archiveNote(data.case) && (
            <p
              className={cn(
                textStyle.meta,
                "flex items-start gap-1.5 rounded-lg border border-amber-500/40 bg-card p-3 text-amber-500",
              )}
            >
              <AlertCircle className="mt-0.5 h-3.5 w-3.5 shrink-0" />
              <span>{archiveNote(data.case)}</span>
            </p>
          )}
          {data?.case && (
            <div className="flex flex-col gap-6">
              <section className="flex flex-col gap-2">
                <h2 className={textStyle.sectionTitle}>Timeline replay</h2>
                <ConversationView conversation={data.case} />
              </section>

              <section className="flex flex-col gap-2">
                <h2 className={textStyle.sectionTitle}>Step ledger</h2>
                <Trajectory doc={fromConversation(data.case)} />
              </section>
            </div>
          )}
        </div>
      </PageShell>
    );
  },
});
