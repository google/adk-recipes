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

/**
 * The control guards a sweep that costs money, so these check what it does with
 * the list it is given -- not that it renders.
 *
 * `lib/__tests__/runstate.test.ts` covers the predicates underneath. What is
 * left to pin here is the wiring: that the guard follows the *list* rather than
 * the click, that a refused start says so, and that the note never contradicts
 * the button beside it.
 */
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { RunInvestigationButton } from "../run-investigation-button";
import type { Run } from "@/lib/aqua-api";

const NOW = Date.now();

function run(status: string, minutesAgo: number): Run {
  return {
    run_id: `${status}-${minutesAgo}`,
    observed_agent_name: "travel_desk_agent",
    created_at: new Date(NOW - minutesAgo * 60_000).toISOString(),
    finished_at: null,
    status,
    elapsed_seconds: null,
    metrics_passed: 0,
    metrics_failed: 0,
    metrics_errored: 0,
    error: null,
  };
}

/** Serves `runs` to every GET and `post` to the POST. */
function mount(
  runs: Run[],
  post?: { body: unknown; status?: number } | "reject",
) {
  const fetchMock = vi.fn(async (_url: unknown, init?: RequestInit) => {
    if (init?.method === "POST") {
      if (post === "reject") throw new TypeError("Failed to fetch");
      return new Response(JSON.stringify(post?.body ?? {}), {
        status: post?.status ?? 200,
      });
    }
    return new Response(JSON.stringify({ runs }), { status: 200 });
  });
  vi.stubGlobal("fetch", fetchMock);

  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  render(
    <QueryClientProvider client={client}>
      <RunInvestigationButton />
    </QueryClientProvider>,
  );
  return fetchMock;
}

const button = () => screen.getByRole("button", { name: "Run investigation" });

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("what the list says", () => {
  it("offers the run when nothing is in flight", async () => {
    mount([run("done", 30)]);
    await waitFor(() => expect(button()).toBeEnabled());
    expect(button()).toHaveTextContent("Run investigation");
  });

  it("is disabled while a sweep is in flight, and names how many", async () => {
    mount([run("running", 2), run("pending", 1)]);
    await waitFor(() => expect(button()).toBeDisabled());
    expect(button()).toHaveTextContent("2 running…");
    expect(screen.getByText("Checking every 30s")).toBeInTheDocument();
  });

  it("stays available for a sweep nobody is waiting on, and says why", async () => {
    // A wedged `running` row would otherwise disable the only control that
    // starts a sweep, permanently and across reloads.
    mount([run("running", 90)]);
    // `findBy`, not `getBy`: an enabled button is also the pre-load state, so
    // waiting on that alone would assert before the list has arrived.
    expect(
      await screen.findByText(/Last investigation stuck for 1h/),
    ).toBeInTheDocument();
    expect(button()).toBeEnabled();
  });

  it("counts a sweep this tab did not start", async () => {
    // The guard is the list, not the click: an ambient or cron-started sweep
    // has to disable the button just as a clicked one does.
    mount([run("running", 1)]);
    await waitFor(() => expect(button()).toBeDisabled());
    expect(button()).toHaveTextContent("Running…");
  });
});

describe("starting one", () => {
  it("posts to /api/investigations and re-reads the list", async () => {
    const fetchMock = mount([run("done", 30)]);
    await waitFor(() => expect(button()).toBeEnabled());

    await userEvent.click(button());

    await waitFor(() =>
      expect(
        fetchMock.mock.calls.some(
          ([url, init]) =>
            String(url) === "/api/investigations" &&
            (init as RequestInit | undefined)?.method === "POST",
        ),
      ).toBe(true),
    );
    // The list is what says whether a sweep is really going, so it is re-read
    // whatever the POST answered.
    await waitFor(() =>
      expect(
        fetchMock.mock.calls.filter(
          ([, init]) => (init as RequestInit | undefined)?.method !== "POST",
        ).length,
      ).toBeGreaterThan(1),
    );
  });

  it("reports an agent that refused, and hands the button back", async () => {
    mount([run("done", 30)], { body: { error: "no telemetry in the window" } });
    await waitFor(() => expect(button()).toBeEnabled());

    await userEvent.click(button());

    expect(
      await screen.findByText(/no telemetry in the window/),
    ).toBeInTheDocument();
    // Nothing was started, so there is nothing to wait for.
    await waitFor(() => expect(button()).toBeEnabled());
  });

  it("reports an agent that did not answer", async () => {
    mount([run("done", 30)], "reject");
    await waitFor(() => expect(button()).toBeEnabled());

    await userEvent.click(button());

    expect(await screen.findByText(/didn't answer/)).toBeInTheDocument();
  });

  it("drops a stale error once the list proves a sweep is going", async () => {
    // The error was only ever a guess about whether the start got through. The
    // list settles it, and a red note beside a "Running…" button contradicts
    // it. This is the real shape of the case: the POST reported a failure and
    // the sweep started anyway.
    const runs = [run("done", 30)];
    const fetchMock = vi.fn(async (_url: unknown, init?: RequestInit) => {
      if (init?.method === "POST") {
        runs.push(run("running", 0));
        return new Response(JSON.stringify({ error: "boom" }), { status: 200 });
      }
      return new Response(JSON.stringify({ runs }), { status: 200 });
    });
    vi.stubGlobal("fetch", fetchMock);

    const client = new QueryClient({
      defaultOptions: {
        queries: { retry: false },
        mutations: { retry: false },
      },
    });
    render(
      <QueryClientProvider client={client}>
        <RunInvestigationButton />
      </QueryClientProvider>,
    );

    await waitFor(() =>
      expect(button()).toHaveTextContent("Run investigation"),
    );
    await userEvent.click(button());

    // The re-read that `onSettled` triggers is what supersedes the error.
    await waitFor(() => expect(button()).toBeDisabled());
    expect(button()).toHaveTextContent("Running…");
    expect(screen.queryByText(/boom/)).not.toBeInTheDocument();
  });

  it("shows Starting… while the POST /api/runs request is in flight", async () => {
    let resolvePost!: (res: Response) => void;
    const fetchMock = vi.fn((_url: unknown, init?: RequestInit) => {
      if (init?.method === "POST") {
        return new Promise<Response>((r) => {
          resolvePost = r;
        });
      }
      return Promise.resolve(
        new Response(JSON.stringify({ runs: [] }), { status: 200 }),
      );
    });
    vi.stubGlobal("fetch", fetchMock);

    const client = new QueryClient({
      defaultOptions: {
        queries: { retry: false },
        mutations: { retry: false },
      },
    });
    render(
      <QueryClientProvider client={client}>
        <RunInvestigationButton />
      </QueryClientProvider>,
    );

    await waitFor(() =>
      expect(button()).toHaveTextContent("Run investigation"),
    );
    await userEvent.click(button());

    expect(button()).toBeDisabled();
    expect(button()).toHaveTextContent("Starting…");
    resolvePost(
      new Response(JSON.stringify({ run_id: "r-new", status: "running" }), {
        status: 200,
      }),
    );
  });
});
