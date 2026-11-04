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
import { trajectoryBundleQuery, type TrajectoryBundle } from "@/lib/aqua-api";
import { TrajectoryBackLink } from "@/components/investigations/trajectory-back-link";
import { conversationToTrajectoryDoc } from "@/lib/trajectory/conversationToTrajectoryDoc";
import { Trajectory } from "@/components/trajectory/trajectory";
import { ConversationView } from "@/components/conversation/conversation-view";
import { PageShell } from "@/components/nav/page-shell";
import { Loading } from "@/components/ui/loading";
import { PageHeader } from "@/components/nav/page-header";
import { link, mono, textStyle } from "@/lib/typography";
import { cn } from "@/lib/utils";
import { useAgentKey } from "@/lib/agent-selection";

/** Why a trajectory bundle is incomplete, or null when all steps are present.
 *
 * Distinguishes three states that an empty or partial timeline cannot show on
 * its own: no bundle was stored (`not_archived`), the step rows expired while
 * the manifest remained (`step_count > steps_returned`), or the bundle was
 * built from incomplete or oversized telemetry (`partial` / `truncated`). */
export function archiveNote(archived: TrajectoryBundle): string | null {
  if (archived.status === "not_archived") {
    return (
      "This trajectory was never archived. It was sampled before the " +
      "trajectory store existed, or its investigation could not assemble it."
    );
  }
  if (archived.step_count > archived.steps_returned) {
    if (archived.steps_returned === 0) {
      return (
        `The archived copy has expired: ${archived.step_count} ${archived.step_count === 1 ? "turn was" : "turns were"} ` +
        "recorded and none are still stored. The trajectory is gone; this " +
        "page is what is left of it."
      );
    }
    return (
      `Partial archive: ${archived.steps_returned} of ${archived.step_count} recorded ` +
      `${archived.step_count === 1 ? "turn is" : "turns are"} still stored.`
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

export const Route = createFileRoute(
  "/investigations/$investigationId/trajectories/$trajectoryId",
)({
  component: function TrajectoryBundleRoute() {
    const { investigationId, trajectoryId } = Route.useParams();
    const agentKey = useAgentKey();
    const { data, isLoading, isError, error } = useQuery(
      trajectoryBundleQuery(agentKey, investigationId, trajectoryId),
    );

    return (
      <PageShell>
        <div className="flex flex-col gap-5">
          <TrajectoryBackLink investigationId={investigationId} />

          <PageHeader
            title={
              <>
                Trajectory{" "}
                <span className={cn(mono, "break-all")}>{trajectoryId}</span>
              </>
            }
          >
            {data?.trajectory_bundle?.trajectory?.console_url && (
              <a
                href={data.trajectory_bundle.trajectory.console_url}
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
          {data?.trajectory_bundle && archiveNote(data.trajectory_bundle) && (
            <p
              className={cn(
                textStyle.meta,
                "flex items-start gap-1.5 rounded-lg border border-amber-500/40 bg-card p-3 text-amber-500",
              )}
            >
              <AlertCircle className="mt-0.5 h-3.5 w-3.5 shrink-0" />
              <span>{archiveNote(data.trajectory_bundle)}</span>
            </p>
          )}
          {data?.trajectory_bundle && (
            <div className="flex flex-col gap-6">
              <section className="flex flex-col gap-2">
                <h2 className={textStyle.sectionTitle}>Timeline replay</h2>
                <ConversationView conversation={data.trajectory_bundle} />
              </section>

              <section className="flex flex-col gap-2">
                <h2 className={textStyle.sectionTitle}>Step ledger</h2>
                <Trajectory
                  doc={conversationToTrajectoryDoc(data.trajectory_bundle)}
                />
              </section>
            </div>
          )}
        </div>
      </PageShell>
    );
  },
});
