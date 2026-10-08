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

import { useEffect } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { prefetchAgentCard } from "./a2a-client";
import {
  dailyQuery,
  effectiveConfigQuery,
  goalQuery,
  INSIGHTS_PAGE_SIZE,
  insightsQuery,
  memoriesQuery,
  runsQuery,
  statsQuery,
} from "./aqua-api";

/** Warm every tab's data once, at boot.
 *
 * Each of these is one HTTP call the dashboard makes anyway the moment you
 * open the tab; doing them up front means switching tabs renders from cache
 * instead of showing a spinner. They are deliberately fired together rather
 * than awaited in sequence -- nothing here depends on anything else, and the
 * slowest one should not delay the other three.
 *
 * `staleTime` on each query (30s) is what stops the tab switch itself from
 * triggering a refetch; without it the cache would be served *and* revalidated,
 * which repaints a loading state on a page that already has data.
 */
export function usePrefetchAqua(): void {
  const queryClient = useQueryClient();

  useEffect(() => {
    prefetchAgentCard();
    // The issue list's own first page, argument for argument: a page warmed
    // under any other size or order is a page it will never ask for.
    void queryClient.prefetchQuery(
      insightsQuery({ orderBy: "impact", pageSize: INSIGHTS_PAGE_SIZE }),
    );
    void queryClient.prefetchQuery(statsQuery());
    void queryClient.prefetchQuery(dailyQuery());
    void queryClient.prefetchQuery(runsQuery());
    // The config page's three reads. They are the slowest in the app (each is
    // a GCS or agent round trip) and the page is small, so warming them here
    // is the difference between it opening instantly and taking seconds.
    void queryClient.prefetchQuery(goalQuery());
    void queryClient.prefetchQuery(memoriesQuery());
    void queryClient.prefetchQuery(effectiveConfigQuery());
  }, [queryClient]);
}
