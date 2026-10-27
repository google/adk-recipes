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
import { act, render, screen, waitFor } from "@testing-library/react";
import {
  isConversationShown,
  parseAgentSearch,
  useAgentSelection,
  buildAgentHref,
} from "../agent-selection";
import { renderWithAgent, stubAgentsFetch } from "./render-with-agent";

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("parseAgentSearch", () => {
  it("reads the key, trimmed", () => {
    expect(parseAgentSearch({ agent: " acme-prod__support " })).toBe(
      "acme-prod__support",
    );
  });

  it.each([{}, { agent: "" }, { agent: "  " }, { agent: 7 }])(
    "selects no agent for %j",
    (search) => {
      expect(parseAgentSearch(search)).toBeUndefined();
    },
  );
});

describe("buildAgentHref", () => {
  it("adds the agent to a dashboard path, keeping its query and fragment", () => {
    expect(buildAgentHref("/insights?status=new#top", "acme-prod__s")).toBe(
      "/insights?status=new&agent=acme-prod__s#top",
    );
  });

  it("leaves a path that names an agent alone", () => {
    expect(buildAgentHref("/insights?agent=other", "acme-prod__s")).toBe(
      "/insights?agent=other",
    );
  });

  it("leaves the path alone for the default agent", () => {
    expect(buildAgentHref("/insights/i-1", undefined)).toBe("/insights/i-1");
  });
});

describe("isConversationShown", () => {
  const prod = { shownKey: "acme-prod__s", defaultKey: "s" };
  const defaultShown = { shownKey: "s", defaultKey: "s" };

  it("lists a conversation with the agent it names", () => {
    expect(isConversationShown("acme-prod__s", prod)).toBe(true);
    expect(isConversationShown("acme-prod__s", defaultShown)).toBe(false);
  });

  it("lists a conversation that names no agent with the default agent", () => {
    expect(isConversationShown(undefined, defaultShown)).toBe(true);
    expect(isConversationShown(undefined, prod)).toBe(false);
  });
});

function SelectionProbe() {
  const { selectedKey, shownKey, defaultKey, agents } = useAgentSelection();
  return (
    <output>
      {JSON.stringify({ selectedKey, shownKey, defaultKey, n: agents.length })}
    </output>
  );
}

const readProbe = () =>
  JSON.parse(screen.getByRole("status").textContent ?? "{}") as Record<
    string,
    unknown
  >;

describe("AgentSelectionProvider", () => {
  it("shows the agent the URL selects", async () => {
    vi.stubGlobal(
      "fetch",
      stubAgentsFetch([{ agent_key: "s" }, { agent_key: "acme-prod__s" }], "s"),
    );

    renderWithAgent(<SelectionProbe />, { url: "/?agent=acme-prod__s" });

    await waitFor(() => expect(readProbe().n).toBe(2));
    expect(readProbe()).toEqual({
      selectedKey: "acme-prod__s",
      shownKey: "acme-prod__s",
      defaultKey: "s",
      n: 2,
    });
  });

  it("shows the default agent once the list names it, selecting none", async () => {
    vi.stubGlobal("fetch", stubAgentsFetch([{ agent_key: "s" }], "s"));

    renderWithAgent(<SelectionProbe />);

    await waitFor(() => expect(readProbe().shownKey).toBe("s"));
    expect(readProbe().selectedKey).toBeUndefined();
  });

  it("selects no agent outside the provider", () => {
    render(<SelectionProbe />);

    expect(readProbe()).toEqual({ n: 0 });
  });

  it("keeps the agent across a navigation that does not name one", async () => {
    vi.stubGlobal("fetch", stubAgentsFetch([], null));
    const { router } = renderWithAgent(<SelectionProbe />, {
      url: "/?agent=acme-prod__s",
      paths: ["/", "/insights"],
    });
    await waitFor(() => expect(readProbe().selectedKey).toBe("acme-prod__s"));

    await act(() => router.navigate({ to: "/insights", search: {} }));

    expect(router.state.location.pathname).toBe("/insights");
    expect(router.state.location.search).toEqual({ agent: "acme-prod__s" });
    expect(readProbe().selectedKey).toBe("acme-prod__s");
  });
});
