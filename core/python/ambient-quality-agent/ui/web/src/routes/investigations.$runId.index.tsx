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
import { InvestigationDetailView } from "@/components/investigations/investigation-detail-view";
import { PageShell } from "@/components/nav/page-shell";

export const Route = createFileRoute("/investigations/$runId/")({
  component: function RunDetailRoute() {
    const { runId } = Route.useParams();
    return (
      <PageShell>
        <InvestigationDetailView runId={runId} />
      </PageShell>
    );
  },
});
