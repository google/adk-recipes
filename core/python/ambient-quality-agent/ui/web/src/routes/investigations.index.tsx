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
import { InvestigationsView } from "@/components/investigations/investigations-view";
import { PageShell } from "@/components/nav/page-shell";
import { parseDay } from "@/lib/day-buckets";

export const Route = createFileRoute("/investigations/")({
  // "Only with failures" lives in the URL, so the filtered list can be linked,
  // reloaded, and returned to with Back from a page it led to. Off is no param
  // at all, which makes every link here that carries none (the sidebar's among
  // them) turn it off.
  //
  // `day` narrows the list to the runs one column of the per-day chart counts.
  validateSearch: (
    search: Record<string, unknown>,
  ): { failures?: "only"; day?: string } => ({
    failures: search.failures === "only" ? "only" : undefined,
    day: parseDay(search.day),
  }),
  component: function Page() {
    const { failures, day } = Route.useSearch();
    const navigate = Route.useNavigate();
    return (
      <PageShell width="wide">
        <InvestigationsView
          failuresOnly={failures === "only"}
          // Replaced rather than pushed, so Back leaves the page instead of
          // undoing the checkbox.
          onFailuresOnlyChange={(only) =>
            void navigate({
              search: (prev) => ({
                ...prev,
                failures: only ? "only" : undefined,
              }),
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
