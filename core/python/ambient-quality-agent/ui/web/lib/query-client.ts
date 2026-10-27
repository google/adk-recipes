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

import { QueryClient } from "@tanstack/react-query";

import { hydrateQueryCache, persistQueryCache } from "./query-persist";

export function makeQueryClient(): QueryClient {
  return new QueryClient({
    defaultOptions: {
      queries: {
        staleTime: 0,
        refetchOnWindowFocus: false,
        // A 501 or a 404 will not become a 200 on the fourth attempt. The
        // unimplemented /lha/* stubs answer 501, and retrying them kept the
        // network busy so Playwright's networkidle never settled.
        retry: (failureCount, error) => {
          const status = (error as { status?: number } | null)?.status;
          const fromMessage = Number(
            /-> (\d{3})$/.exec((error as Error | null)?.message ?? "")?.[1],
          );
          const code =
            status ?? (Number.isFinite(fromMessage) ? fromMessage : undefined);
          // 4xx is a bad request and 501 is not implemented; neither improves
          // with another attempt.
          if (
            code !== undefined &&
            (code === 501 || (code >= 400 && code < 500))
          ) {
            return false;
          }
          return failureCount < 3;
        },
      },
    },
  });
}

export const queryClient = makeQueryClient();

// Hydrate before the first render so the first paint comes from disk, then
// keep writing. Module scope on purpose: a React effect would run after the
// tree has already mounted and rendered its empty states.
hydrateQueryCache(queryClient);
persistQueryCache(queryClient);
