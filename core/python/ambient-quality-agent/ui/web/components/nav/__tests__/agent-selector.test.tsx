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

import { afterEach, describe, expect, it, vi } from "vitest";
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import {
  renderWithAgent,
  stubAgentsFetch,
} from "@/lib/__tests__/render-with-agent";
import { AgentSelector, resolveAgentSwitchPath } from "../agent-selector";

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("resolveAgentSwitchPath", () => {
  it.each([
    ["/", "/"],
    ["/insights", "/insights"],
    ["/insights/i-1", "/insights"],
    ["/investigations/r-1/cases/c-1", "/investigations"],
    ["/config", "/config"],
    ["/c", "/c"],
  ])("leaves %s for %s", (from, to) => {
    expect(resolveAgentSwitchPath(from)).toBe(to);
  });
});

const PATHS = ["/", "/insights", "/insights/$insightId"];

describe("AgentSelector", () => {
  it("is the badge alone with one agent attached", async () => {
    vi.stubGlobal(
      "fetch",
      stubAgentsFetch([{ agent_key: "support" }], "support"),
    );

    renderWithAgent(<AgentSelector fallbackName="" />, { paths: PATHS });

    const badge = await screen.findByTestId("agent-freshness-badge");
    await waitFor(() => expect(badge).toHaveTextContent("support"));
    expect(screen.queryByRole("button")).toBeNull();
  });

  it("names the agent from the runs before the list loads", () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(() => new Promise<Response>(() => {})),
    );

    renderWithAgent(<AgentSelector fallbackName="it_support_agent" />, {
      paths: PATHS,
    });

    return waitFor(() =>
      expect(screen.getByTestId("agent-freshness-badge")).toHaveTextContent(
        "it_support_agent",
      ),
    );
  });

  it("tells apart one agent in two projects", async () => {
    vi.stubGlobal(
      "fetch",
      stubAgentsFetch(
        [
          { agent_key: "acme-staging__support" },
          { agent_key: "acme-prod__support" },
        ],
        "acme-staging__support",
      ),
    );
    renderWithAgent(<AgentSelector fallbackName="" />, {
      url: "/?agent=acme-prod__support",
      paths: PATHS,
    });

    const badge = await screen.findByRole("button", { name: /switch agent/i });
    expect(badge).toHaveTextContent("supportacme-prod");
    await userEvent.click(badge);

    const items = within(
      await screen.findByRole("list", { name: "Attached agents" }),
    ).getAllByRole("button");
    expect(items.map((item) => item.textContent)).toEqual([
      "supportacme-staging · default",
      "supportacme-prod",
    ]);
    expect(items[1]).toHaveAttribute("aria-current", "true");
  });

  it("opens the next agent on the list the detail page came from", async () => {
    vi.stubGlobal(
      "fetch",
      stubAgentsFetch(
        [{ agent_key: "support" }, { agent_key: "acme-prod__support" }],
        "support",
      ),
    );
    const { router } = renderWithAgent(<AgentSelector fallbackName="" />, {
      url: "/insights/i-1",
      paths: PATHS,
    });

    await userEvent.click(
      await screen.findByRole("button", { name: /switch agent/i }),
    );
    await userEvent.click(
      await screen.findByRole("button", { name: /acme-prod/ }),
    );

    await waitFor(() =>
      expect(router.state.location.href).toBe(
        "/insights?agent=acme-prod__support",
      ),
    );
  });

  it("drops the parameter when switching back to the default agent", async () => {
    vi.stubGlobal(
      "fetch",
      stubAgentsFetch(
        [{ agent_key: "support" }, { agent_key: "acme-prod__support" }],
        "support",
      ),
    );
    const { router } = renderWithAgent(<AgentSelector fallbackName="" />, {
      url: "/insights?agent=acme-prod__support",
      paths: PATHS,
    });

    await userEvent.click(
      await screen.findByRole("button", { name: /switch agent/i }),
    );
    await userEvent.click(
      await screen.findByRole("button", { name: /default/ }),
    );

    await waitFor(() => expect(router.state.location.href).toBe("/insights"));
  });
});
