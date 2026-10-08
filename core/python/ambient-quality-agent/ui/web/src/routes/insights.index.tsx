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

import { createFileRoute } from "@tanstack/react-router";
import {
  INSIGHT_STATUS_FILTERS,
  InsightsView,
  type InsightStatusFilter,
} from "@/components/insights/insights-view";
import { PageShell } from "@/components/nav/page-shell";
import { parseDay } from "@/lib/day-buckets";

export const Route = createFileRoute("/insights/")({
  // Every layer below this already filtered by run -- `insightsQuery({runId})`,
  // the `?runId=` param on `ui/app.py`, and an EXISTS over
  // `insight_occurrences.run_id` in the reader. The route was the only one that
  // could not say it, so one sweep's findings had no address of their own.
  //
  // The status filter lives here too, so a filtered list can be linked,
  // reloaded, and returned to with Back from a page it led to. A status the
  // list has no tab for is dropped rather than kept: kept, it would ask the
  // engine for a filter that matches nothing and draw no tab as selected.
  //
  // `day` narrows the list to what one column of the per-day chart counts: the
  // insights found or resolved that UTC day. Recurring is dropped beside it,
  // because no day is stamped with a recurrence and the chart has no such
  // series.
  validateSearch: (
    search: Record<string, unknown>,
  ): { runId?: string; status?: InsightStatusFilter; day?: string } => {
    const day = parseDay(search.day);
    const status = INSIGHT_STATUS_FILTERS.find((s) => s === search.status);
    return {
      runId: typeof search.runId === "string" ? search.runId : undefined,
      status: day && status === "recurring" ? undefined : status,
      day,
    };
  },
  component: function Page() {
    const { runId, status, day } = Route.useSearch();
    const navigate = Route.useNavigate();
    return (
      <PageShell>
        <InsightsView
          runId={runId}
          status={status}
          // Replaced rather than pushed, so Back leaves the page instead of
          // stepping through every tab that was tried on it.
          onStatusChange={(next) =>
            void navigate({
              search: (prev) => ({ ...prev, status: next }),
              replace: true,
            })
          }
          day={day}
          onDayClear={() =>
            void navigate({
              search: (prev) => ({ ...prev, day: undefined }),
              replace: true,
            })
          }
        />
      </PageShell>
    );
  },
});
