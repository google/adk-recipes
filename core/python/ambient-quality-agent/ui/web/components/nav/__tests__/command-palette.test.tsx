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
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, fireEvent, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MessageSquarePlus } from "lucide-react";
import type { ReactNode } from "react";

const navigate = vi.fn();
vi.mock("@tanstack/react-router", () => ({
  useNavigate: () => navigate,
}));

import { buildAgentScope } from "@/lib/aqua-api";
import {
  type PaletteCommand,
  setCommandPaletteOpen,
  useRegisterPaletteCommands,
} from "@/lib/command-palette";
import { CommandPalette } from "../command-palette";

const INSIGHT_ID = "0372c5aa1133444091d572fe9727bc69";
const INVESTIGATION_ID = "31fd3807a9c24e0b8f1d2c3b4a596877";

/** Answers the two detail reads, where `insights` and `investigations` are the
 *  ids each knows, and the insight search over `searchable`. Anything else is
 *  the error `ui/app.py` returns. */
function stubDetailReads(
  insights: string[],
  investigations: string[],
  searchable: { insight_id: string; label: string }[] = [],
): ReturnType<typeof vi.fn> {
  const fetchMock = vi.fn((input: RequestInfo | URL) => {
    const url = new URL(String(input), "http://x");
    const id = decodeURIComponent(url.pathname.split("/").pop() ?? "");
    let body: unknown = { error: "not found" };
    if (url.pathname === "/api/insights") {
      const q = (url.searchParams.get("q") ?? "").toLowerCase();
      const hits = searchable.filter(
        (i) =>
          i.label.toLowerCase().includes(q) ||
          i.insight_id.toLowerCase().startsWith(q),
      );
      body = {
        insights: hits.map((i) => ({ ...i, diagnosis: "" })),
        total: hits.length,
      };
    }
    if (url.pathname.startsWith("/api/insights/") && insights.includes(id)) {
      body = {
        insight: { insight_id: id, label: "create_ticket fails" },
        occurrences: [],
        root_causes: [],
      };
    }
    if (
      url.pathname.startsWith("/api/investigations/") &&
      investigations.includes(id)
    ) {
      body = { investigation: { investigation_id: id, status: "done" } };
    }
    return Promise.resolve(new Response(JSON.stringify(body)));
  });
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

function renderPalette(client = new QueryClient(), extra?: ReactNode) {
  render(
    <QueryClientProvider client={client}>
      {extra}
      <CommandPalette />
    </QueryClientProvider>,
  );
  return client;
}

function openPalette() {
  act(() => setCommandPaletteOpen(true));
  return screen.getByRole("textbox");
}

describe("CommandPalette", () => {
  beforeEach(() => {
    navigate.mockReset();
    stubDetailReads([], []);
  });
  afterEach(() => {
    act(() => setCommandPaletteOpen(false));
    vi.unstubAllGlobals();
  });

  it("opens on Ctrl+K and Cmd+K wherever focus is", () => {
    renderPalette();
    expect(screen.queryByRole("dialog")).toBeNull();
    fireEvent.keyDown(window, { key: "k", ctrlKey: true });
    expect(screen.getByRole("dialog")).toBeInTheDocument();
    fireEvent.keyDown(window, { key: "k", metaKey: true });
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("opens an insight by its full id when the server knows it", async () => {
    stubDetailReads([INSIGHT_ID], []);
    renderPalette();
    await userEvent.type(openPalette(), INSIGHT_ID);

    const option = await screen.findByRole("option", {
      name: /create_ticket fails/,
    });
    expect(option).toHaveTextContent("Insight 0372c5aa");
    expect(screen.queryByRole("option", { name: /^Investigation/ })).toBeNull();
    await userEvent.click(option);
    expect(navigate).toHaveBeenCalledWith({
      to: "/insights/$insightId",
      params: { insightId: INSIGHT_ID },
    });
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("opens an investigation by its full id when the server knows it", async () => {
    stubDetailReads([], [INVESTIGATION_ID]);
    renderPalette();
    await userEvent.type(openPalette(), INVESTIGATION_ID);

    await userEvent.click(
      await screen.findByRole("option", { name: /Investigation 31fd3807/ }),
    );
    expect(navigate).toHaveBeenCalledWith({
      to: "/investigations/$investigationId",
      params: { investigationId: INVESTIGATION_ID },
    });
  });

  it("says so when neither kind has the id", async () => {
    renderPalette();
    await userEvent.type(openPalette(), "deadbeef0000");
    expect(
      await screen.findByText(
        /^No insight or investigation with ID deadbeef0000\./,
      ),
    ).toBeInTheDocument();
  });

  it("expands the short id the dashboard shows from loaded investigations", async () => {
    const fetchMock = stubDetailReads([], []);
    const client = new QueryClient();
    client.setQueryData(
      [...buildAgentScope(undefined), "investigations"],
      [{ investigation_id: INVESTIGATION_ID, status: "done" }],
    );
    renderPalette(client);
    await userEvent.type(openPalette(), "31fd3807");

    await userEvent.keyboard("{Enter}");
    expect(navigate).toHaveBeenCalledWith({
      to: "/investigations/$investigationId",
      params: { investigationId: INVESTIGATION_ID },
    });
    // The server reads only whole ids, so a loaded match ends the search.
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("calls an investigation stalled when the table would", async () => {
    const client = new QueryClient();
    client.setQueryData(
      [...buildAgentScope(undefined), "investigations"],
      [
        {
          investigation_id: INVESTIGATION_ID,
          // The job died hours ago, so the record still says running.
          status: "running",
          created_at: new Date(Date.now() - 4 * 3600_000).toISOString(),
        },
      ],
    );
    renderPalette(client);
    await userEvent.type(openPalette(), "31fd3807");
    expect(
      screen.getByRole("option", { name: /Investigation 31fd3807/ }),
    ).toHaveTextContent("stalled");
  });

  it("finds an insight the dashboard never loaded by searching the server", async () => {
    const fetchMock = stubDetailReads(
      [],
      [],
      [{ insight_id: "ins-far-down", label: "books the same hotel twice" }],
    );
    renderPalette();
    await userEvent.type(openPalette(), "hotel");

    const option = await screen.findByRole("option", {
      name: /books the same hotel twice/,
    });
    expect(option).toHaveTextContent("Insight ins-far-");
    const searchUrl = fetchMock.mock.calls
      .map(([input]) => new URL(String(input), "http://x"))
      .find((u) => u.pathname === "/api/insights");
    expect(searchUrl?.searchParams.get("q")).toBe("hotel");
    await userEvent.click(option);
    expect(navigate).toHaveBeenCalledWith({
      to: "/insights/$insightId",
      params: { insightId: "ins-far-down" },
    });
  });

  it("does not search the server for a single character", async () => {
    const fetchMock = stubDetailReads([], []);
    renderPalette();
    await userEvent.type(openPalette(), "h");
    await new Promise((r) => setTimeout(r, 400));
    expect(
      fetchMock.mock.calls.some(
        ([input]) =>
          new URL(String(input), "http://x").pathname === "/api/insights",
      ),
    ).toBe(false);
  });

  it("searches loaded insights by label", async () => {
    const client = new QueryClient();
    client.setQueryData([...buildAgentScope(undefined), "insights"], {
      insights: [
        { insight_id: INSIGHT_ID, label: "planner loops", diagnosis: "" },
      ],
      total: 1,
    });
    renderPalette(client);
    await userEvent.type(openPalette(), "planner");
    expect(
      screen.getByRole("option", { name: /planner loops/ }),
    ).toHaveTextContent("Insight 0372c5aa");
  });

  it("opens an id as the kind chosen with Open … by ID", async () => {
    const fetchMock = stubDetailReads([], []);
    renderPalette();
    openPalette();
    await userEvent.click(
      screen.getByRole("option", { name: "Open investigation by ID…" }),
    );
    await userEvent.type(
      screen.getByPlaceholderText("Paste an investigation ID…"),
      "abc12345{Enter}",
    );
    expect(navigate).toHaveBeenCalledWith({
      to: "/investigations/$investigationId",
      params: { investigationId: "abc12345" },
    });
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("selects a result that arrives after ArrowDown on an empty list", async () => {
    stubDetailReads([], [INVESTIGATION_ID]);
    renderPalette();
    const input = openPalette();
    await userEvent.type(input, INVESTIGATION_ID);
    // Pressed while the id is still being looked up and nothing is listed.
    await userEvent.keyboard("{ArrowDown}");
    await screen.findByRole("option", { name: /Investigation 31fd3807/ });
    await userEvent.keyboard("{Enter}");
    expect(navigate).toHaveBeenCalledWith({
      to: "/investigations/$investigationId",
      params: { investigationId: INVESTIGATION_ID },
    });
  });

  it("does not offer to open an id that matches a loaded item in another case", async () => {
    const client = new QueryClient();
    client.setQueryData(
      [...buildAgentScope(undefined), "investigations"],
      [{ investigation_id: INVESTIGATION_ID, status: "done" }],
    );
    renderPalette(client);
    openPalette();
    await userEvent.click(
      screen.getByRole("option", { name: "Open investigation by ID…" }),
    );
    await userEvent.keyboard(INVESTIGATION_ID.toUpperCase());
    expect(screen.getAllByRole("option")).toHaveLength(1);
    expect(
      screen.getByRole("option", { name: /Investigation 31fd3807/ }),
    ).toBeInTheDocument();
  });

  it("puts focus back in the input after Open … by ID is clicked", async () => {
    renderPalette();
    openPalette();
    await userEvent.click(
      screen.getByRole("option", { name: "Open insight by ID…" }),
    );
    expect(screen.getByPlaceholderText("Paste an insight ID…")).toHaveFocus();
  });

  it("keeps a page's commands in mount order when they update", () => {
    const first = (label: string): PaletteCommand[] => [
      { id: label, label, Icon: MessageSquarePlus, onSelect: () => {} },
    ];
    function Page({ commands }: { commands: PaletteCommand[] }) {
      useRegisterPaletteCommands(commands);
      return null;
    }
    const client = new QueryClient();
    const a1 = first("Alpha");
    const b = first("Beta");
    const tree = (a: PaletteCommand[]) => (
      <QueryClientProvider client={client}>
        <Page commands={a} />
        <Page commands={b} />
        <CommandPalette />
      </QueryClientProvider>
    );
    const { rerender } = render(tree(a1));
    rerender(tree(first("Alpha 2")));
    openPalette();
    const labels = screen.getAllByRole("option").map((o) => o.textContent);
    expect(labels.indexOf("Alpha 2")).toBeLessThan(labels.indexOf("Beta"));
  });

  it("lists the commands a mounted page registers, and drops them on unmount", async () => {
    const onSelect = vi.fn();
    const commands: PaletteCommand[] = [
      { id: "page", label: "New chat", Icon: MessageSquarePlus, onSelect },
    ];
    function Page() {
      useRegisterPaletteCommands(commands);
      return null;
    }
    const client = new QueryClient();
    const { rerender } = render(
      <QueryClientProvider client={client}>
        <Page />
        <CommandPalette />
      </QueryClientProvider>,
    );
    openPalette();
    await userEvent.click(screen.getByRole("option", { name: /New chat/ }));
    expect(onSelect).toHaveBeenCalled();

    rerender(
      <QueryClientProvider client={client}>
        <CommandPalette />
      </QueryClientProvider>,
    );
    openPalette();
    expect(screen.queryByRole("option", { name: /New chat/ })).toBeNull();
    expect(
      screen.getByRole("option", { name: "Go to Insights" }),
    ).toBeInTheDocument();
  });
});
