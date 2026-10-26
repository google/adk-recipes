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
  type InsightStatusFilter,
  parsePageNumber,
} from "@/components/insights/insights-view";
import { InvestigationDetailView } from "@/components/investigations/investigation-detail-view";
import { PageShell } from "@/components/nav/page-shell";

export const Route = createFileRoute("/investigations/$runId/")({
  // The status tab and page of the run's insights list, so Back from an
  // insight returns to the page it was opened from and a link can name it.
  // They travel together because a page number only means something within
  // the status it was reached under.
  validateSearch: (
    search: Record<string, unknown>,
  ): { status?: InsightStatusFilter; page?: number } => ({
    status: INSIGHT_STATUS_FILTERS.find((s) => s === search.status),
    page: parsePageNumber(search.page),
  }),
  component: function RunDetailRoute() {
    const { runId } = Route.useParams();
    const { status, page } = Route.useSearch();
    const navigate = Route.useNavigate();
    return (
      <PageShell>
        <InvestigationDetailView
          runId={runId}
          insightStatus={status}
          // Replaced rather than pushed, as on the insights page, so Back
          // leaves the run instead of stepping through the list.
          onInsightStatusChange={(next) =>
            void navigate({
              search: (prev) => ({ ...prev, status: next, page: undefined }),
              replace: true,
            })
          }
          insightPage={page}
          onInsightPageChange={(next) =>
            void navigate({
              search: (prev) => ({ ...prev, page: next }),
              replace: true,
            })
          }
        />
      </PageShell>
    );
  },
});
