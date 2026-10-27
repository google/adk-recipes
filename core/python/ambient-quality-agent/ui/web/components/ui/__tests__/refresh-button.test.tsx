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

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useQuery } from "@tanstack/react-query";
import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { buildAgentScope } from "@/lib/aqua-api";
import { useAgentKey } from "@/lib/agent-selection";
import {
  renderWithAgent,
  stubAgentsFetch,
} from "@/lib/__tests__/render-with-agent";
import { PageRefreshButton } from "../refresh-button";

/**
 * A read under the selected agent's scope, standing in for a page's data.
 *
 * Args:
 *   read: Answers the read; its result is shown as "read <result>".
 *
 * Returns:
 *   The read's result, or "pending" until it arrives.
 */
function Probe({ read }: { read: () => Promise<number> }) {
  const agentKey = useAgentKey();
  const { data } = useQuery({
    queryKey: [...buildAgentScope(agentKey), "probe"],
    queryFn: read,
    staleTime: Infinity,
  });
  return <p>read {data ?? "pending"}</p>;
}

describe("PageRefreshButton", () => {
  // The agent selection reads the agent list; nothing here depends on it.
  beforeEach(() => vi.stubGlobal("fetch", stubAgentsFetch([], null)));
  afterEach(() => vi.unstubAllGlobals());

  it("re-reads the data on screen, even when it is not stale", async () => {
    let reads = 0;
    renderWithAgent(
      <>
        <Probe read={async () => ++reads} />
        <PageRefreshButton />
      </>,
    );
    expect(await screen.findByText("read 1")).toBeInTheDocument();

    await userEvent.click(
      screen.getByRole("button", { name: "Refresh the data" }),
    );

    expect(await screen.findByText("read 2")).toBeInTheDocument();
  });

  it("marks the agent's other reads stale, and leaves other agents' alone", async () => {
    const { queryClient } = renderWithAgent(
      <>
        <Probe read={async () => 1} />
        <PageRefreshButton />
      </>,
      { url: "/?agent=acme" },
    );
    await screen.findByText("read 1");
    const offscreen = [...buildAgentScope("acme"), "offscreen"];
    const otherAgent = [...buildAgentScope("other"), "probe"];
    queryClient.setQueryData(offscreen, 1);
    queryClient.setQueryData(otherAgent, 1);

    await userEvent.click(
      screen.getByRole("button", { name: "Refresh the data" }),
    );

    await waitFor(() =>
      expect(queryClient.getQueryState(offscreen)?.isInvalidated).toBe(true),
    );
    expect(queryClient.getQueryState(otherAgent)?.isInvalidated).toBe(false);
  });

  it("is disabled while the re-read is in flight", async () => {
    let finish = () => {};
    let reads = 0;
    renderWithAgent(
      <>
        <Probe
          read={() => {
            reads += 1;
            if (reads === 1) return Promise.resolve(1);
            return new Promise((resolve) => {
              finish = () => resolve(reads);
            });
          }}
        />
        <PageRefreshButton />
      </>,
    );
    await screen.findByText("read 1");
    const button = screen.getByRole("button", { name: "Refresh the data" });
    expect(button).toBeEnabled();

    await userEvent.click(button);
    await waitFor(() => expect(button).toBeDisabled());

    finish();
    await waitFor(() => expect(button).toBeEnabled());
    expect(screen.getByText("read 2")).toBeInTheDocument();
  });
});
