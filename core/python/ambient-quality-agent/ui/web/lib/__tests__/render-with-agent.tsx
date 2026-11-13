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

import type { ReactNode } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render } from "@testing-library/react";
import {
  createMemoryHistory,
  createRootRoute,
  createRoute,
  createRouter,
  Outlet,
  RouterProvider,
  retainSearchParams,
} from "@tanstack/react-router";
import {
  AGENT_SEARCH_PARAM,
  AgentSelectionProvider,
  parseAgentSearch,
} from "../agent-selection";

/**
 * Renders `ui` on every one of `paths` under a root route that keeps the
 * agent search parameter and provides the selection, as the app's root does.
 *
 * @param ui - What each route renders.
 * @param opts - The URL to open, and the paths that render `ui`.
 * @returns The render result, the router and the query client.
 */
export function renderWithAgent(
  ui: ReactNode,
  {
    url = "/",
    paths = ["/"],
  }: {
    url?: string;
    paths?: string[];
  } = {},
) {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: Infinity } },
  });
  const rootRoute = createRootRoute({
    validateSearch: (search: Record<string, unknown>): { agent?: string } => {
      const agent = parseAgentSearch(search);
      return agent ? { agent } : {};
    },
    search: { middlewares: [retainSearchParams([AGENT_SEARCH_PARAM])] },
    component: () => (
      <AgentSelectionProvider>
        <Outlet />
      </AgentSelectionProvider>
    ),
  });
  const routes = paths.map((path) =>
    createRoute({ getParentRoute: () => rootRoute, path, component: () => ui }),
  );
  const router = createRouter({
    routeTree: rootRoute.addChildren(routes),
    history: createMemoryHistory({ initialEntries: [url] }),
  });
  const result = render(
    <QueryClientProvider client={queryClient}>
      <RouterProvider router={router} />
    </QueryClientProvider>,
  );
  return { ...result, router, queryClient };
}

/**
 * A fetch that answers `/api/agents` with `agents` and `defaultKey`, and
 * every other request with `fallback`.
 *
 * @param agents - The attached agents to list.
 * @param defaultKey - The default agent's key.
 * @param fallback - The body of every other response.
 * @returns The fetch stub.
 */
export function stubAgentsFetch(
  agents: Array<{
    agent_key: string;
    agent_name?: string;
    project_id?: string | null;
    default?: boolean;
  }>,
  defaultKey: string | null,
  fallback: (url: string) => unknown = () => ({}),
): typeof fetch {
  return ((input: RequestInfo | URL) => {
    const url = typeof input === "string" ? input : input.toString();
    const body = url.startsWith("/api/agents")
      ? {
          agents: agents.map((a) => ({
            agent_name: a.agent_key.split("__").pop(),
            project_id: a.agent_key.includes("__")
              ? a.agent_key.split("__")[0]
              : null,
            default: a.agent_key === defaultKey,
            ...a,
          })),
          default_agent_key: defaultKey,
        }
      : fallback(url);
    return Promise.resolve(new Response(JSON.stringify(body), { status: 200 }));
  }) as typeof fetch;
}
