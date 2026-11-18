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

import { beforeEach, describe, expect, it, vi } from "vitest";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { SelectorCheck } from "@/lib/aqua-api";
import { SelectorCard } from "../selector-card";

const KEY = "acme-prod__billing";
const SQL =
  "SELECT DISTINCT session_id AS target_id FROM agent_analytics.agent_events";

const CONFIG = {
  agent_key: KEY,
  observed_agent_name: "billing",
  quality_analysis_mode: "session_review",
  telemetry_ingestion_source: "big_query",
  telemetry_dataset: "agent_analytics",
  telemetry_table: "agent_events",
  investigation_selector_sql: "",
};

const STARTER = [
  "SELECT session_id AS target_id",
  "FROM `acme-prod.agent_analytics.agent_events`",
  "WHERE timestamp BETWEEN @window_start AND @window_end",
  "  AND COALESCE(JSON_VALUE(attributes, '$.root_agent_name'), agent) = @agent_name",
  "  AND session_id IS NOT NULL",
  "GROUP BY session_id",
].join("\n");

const STARTER_HINT =
  "This starts as the query the random sample uses. Add conditions to narrow it.";

const REFUSAL =
  "A selector must read the selector table agent_analytics.agent_events " +
  "by name and name no other table.";

function buildCheck(overrides: Partial<SelectorCheck> = {}): SelectorCheck {
  return {
    refusal: "",
    changes: [{ field: "investigation_selector_sql", before: "", after: SQL }],
    ...overrides,
  };
}

interface Sent {
  method: string;
  url: string;
  body: Record<string, unknown>;
}

/** Answers GET /api/config with `config` and `starter`, a check with `check`, and a save
 *  with a write of the selector it received. `answerCheck` and `answerSave`
 *  replace those answers. Records every request body. */
function stubFetch({
  config = CONFIG as Record<string, unknown>,
  starter,
  check = buildCheck(),
  answerCheck,
  answerSave,
}: {
  config?: Record<string, unknown>;
  starter?: string;
  check?: SelectorCheck;
  answerCheck?: () => Promise<Response>;
  answerSave?: () => Promise<Response>;
} = {}) {
  const sent: Sent[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string, init?: RequestInit) => {
      const method = init?.method ?? "GET";
      if (method === "GET") {
        return reply(
          starter === undefined
            ? { config }
            : { config, starter_selector: starter },
        );
      }
      const body = JSON.parse(String(init?.body));
      sent.push({ method, url, body });
      if (url === "/api/selector/check") {
        return answerCheck ? answerCheck() : reply(check);
      }
      if (answerSave) return answerSave();
      return reply({
        written: true,
        changes: [
          {
            field: "investigation_selector_sql",
            before: config.investigation_selector_sql,
            after: body.investigation_selector_sql,
          },
        ],
      });
    }),
  );
  return sent;
}

function reply(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), { status });
}

function renderCard() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: 0 } },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <SelectorCard />
    </QueryClientProvider>,
  );
}

async function enterSelector(
  user: ReturnType<typeof userEvent.setup>,
  sql: string,
) {
  const box = screen.getByRole("textbox", { name: "Selector" });
  await user.clear(box);
  await user.type(box, sql);
}

describe("SelectorCard", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
  });

  it("says investigations review a random sample when no selector is stored", async () => {
    stubFetch();
    renderCard();

    expect(await screen.findByText(/random sample/)).toBeInTheDocument();
    expect(screen.queryByRole("textbox")).toBeNull();
    expect(screen.queryByRole("button", { name: "Clear selector" })).toBeNull();
    expect(
      screen.getByRole("button", { name: "Add selector" }),
    ).toBeInTheDocument();
  });

  it("shows the stored selector read-only", async () => {
    stubFetch({ config: { ...CONFIG, investigation_selector_sql: SQL } });
    renderCard();

    expect(await screen.findByText(SQL)).toBeInTheDocument();
    expect(screen.queryByRole("textbox")).toBeNull();
    expect(
      screen.getByRole("button", { name: "Change selector" }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "Clear selector" }),
    ).toBeInTheDocument();
  });

  it("prefills the editor and names the selector table", async () => {
    stubFetch({ config: { ...CONFIG, investigation_selector_sql: SQL } });
    renderCard();
    const user = userEvent.setup();

    await user.click(
      await screen.findByRole("button", { name: "Change selector" }),
    );

    expect(screen.getByRole("textbox", { name: "Selector" })).toHaveValue(SQL);
    expect(
      screen.getByText("agent_analytics.agent_events"),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "Save selector" }),
    ).toBeDisabled();
  });

  it("starts a new selector from the query the random sample uses", async () => {
    const sent = stubFetch({ starter: STARTER });
    renderCard();
    const user = userEvent.setup();

    await user.click(
      await screen.findByRole("button", { name: "Add selector" }),
    );

    expect(screen.getByRole("textbox", { name: "Selector" })).toHaveValue(
      STARTER,
    );
    expect(screen.getByText(STARTER_HINT)).toBeInTheDocument();
    // Nothing is checked or saved until the user asks.
    expect(sent).toEqual([]);
    expect(screen.getByText(/random sample of/)).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "Save selector" }),
    ).toBeDisabled();
  });

  it("starts empty when the agent sends no starter", async () => {
    stubFetch();
    renderCard();
    const user = userEvent.setup();

    await user.click(
      await screen.findByRole("button", { name: "Add selector" }),
    );

    expect(screen.getByRole("textbox", { name: "Selector" })).toHaveValue("");
    expect(screen.queryByText(STARTER_HINT)).toBeNull();
  });

  it("changes the stored selector rather than the starter", async () => {
    stubFetch({
      config: { ...CONFIG, investigation_selector_sql: SQL },
      starter: STARTER,
    });
    renderCard();
    const user = userEvent.setup();

    await user.click(
      await screen.findByRole("button", { name: "Change selector" }),
    );

    expect(screen.getByRole("textbox", { name: "Selector" })).toHaveValue(SQL);
    expect(screen.queryByText(STARTER_HINT)).toBeNull();
  });

  it("names completions_view as the selector table of a cloud_logging source", async () => {
    stubFetch({
      config: {
        ...CONFIG,
        telemetry_ingestion_source: "cloud_logging",
        telemetry_dataset: "logs",
        telemetry_table: "gen_ai_client_inference_operation_details",
      },
    });
    renderCard();
    const user = userEvent.setup();

    await user.click(
      await screen.findByRole("button", { name: "Add selector" }),
    );

    expect(screen.getByText("logs.completions_view")).toBeInTheDocument();
  });

  it("checks the selector, then saves it and shows it stored", async () => {
    const sent = stubFetch();
    renderCard();
    const user = userEvent.setup();

    await user.click(
      await screen.findByRole("button", { name: "Add selector" }),
    );
    await enterSelector(user, SQL);
    await user.click(screen.getByRole("button", { name: "Check selector" }));

    expect(
      await screen.findByText("AQuA can run this selector"),
    ).toBeInTheDocument();
    expect(sent[0]).toEqual({
      method: "POST",
      url: "/api/selector/check",
      body: { agent_key: KEY, investigation_selector_sql: SQL },
    });

    const save = screen.getByRole("button", { name: "Save selector" });
    expect(save).toBeEnabled();
    await user.click(save);

    expect(await screen.findByText("Saved")).toBeInTheDocument();
    expect(sent[1]).toEqual({
      method: "PUT",
      url: "/api/selector",
      body: { agent_key: KEY, investigation_selector_sql: SQL },
    });
    expect(screen.queryByRole("textbox")).toBeNull();
    expect(screen.getByText(SQL)).toBeInTheDocument();
  });

  it("shows the agent's reason for refusing a selector and will not save it", async () => {
    stubFetch({ check: buildCheck({ refusal: REFUSAL, changes: [] }) });
    renderCard();
    const user = userEvent.setup();

    await user.click(
      await screen.findByRole("button", { name: "Add selector" }),
    );
    await enterSelector(user, SQL);
    await user.click(screen.getByRole("button", { name: "Check selector" }));

    expect(
      await screen.findByText("AQuA can't use this selector"),
    ).toBeInTheDocument();
    expect(screen.getByText(REFUSAL)).toBeInTheDocument();
    expect(screen.getByRole("alert")).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "Save selector" }),
    ).toBeDisabled();
  });

  it("asks for a new check once the selector is edited", async () => {
    stubFetch();
    renderCard();
    const user = userEvent.setup();

    await user.click(
      await screen.findByRole("button", { name: "Add selector" }),
    );
    await enterSelector(user, SQL);
    await user.click(screen.getByRole("button", { name: "Check selector" }));
    await screen.findByText("AQuA can run this selector");

    await user.type(screen.getByRole("textbox", { name: "Selector" }), " ");

    expect(
      screen.getByRole("button", { name: "Save selector" }),
    ).toBeDisabled();
    expect(screen.queryByText("AQuA can run this selector")).toBeNull();
    expect(screen.getByText("Check the selector to save.")).toBeInTheDocument();
  });

  it("says why the stored selector cannot be saved again", async () => {
    stubFetch({
      config: { ...CONFIG, investigation_selector_sql: SQL },
      check: buildCheck({ changes: [] }),
    });
    renderCard();
    const user = userEvent.setup();

    await user.click(
      await screen.findByRole("button", { name: "Change selector" }),
    );
    await user.click(screen.getByRole("button", { name: "Check selector" }));

    expect(
      await screen.findByText("This selector is already saved."),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "Save selector" }),
    ).toBeDisabled();
  });

  it("puts the stored selector back on cancel", async () => {
    stubFetch({ config: { ...CONFIG, investigation_selector_sql: SQL } });
    renderCard();
    const user = userEvent.setup();

    await user.click(
      await screen.findByRole("button", { name: "Change selector" }),
    );
    await enterSelector(user, "SELECT 1");
    await user.click(screen.getByRole("button", { name: "Cancel" }));

    expect(screen.queryByRole("textbox")).toBeNull();
    expect(screen.getByText(SQL)).toBeInTheDocument();
  });

  it("clears the stored selector once the user confirms", async () => {
    const sent = stubFetch({
      config: { ...CONFIG, investigation_selector_sql: SQL },
    });
    renderCard();
    const user = userEvent.setup();

    await user.click(
      await screen.findByRole("button", { name: "Clear selector" }),
    );
    expect(sent).toHaveLength(0);
    // Dismissing the confirmation keeps the selector.
    await user.click(screen.getByRole("button", { name: "Keep selector" }));
    expect(sent).toHaveLength(0);
    expect(screen.getByText(SQL)).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Clear selector" }));
    await user.click(screen.getByRole("button", { name: "Clear" }));

    expect(await screen.findByText(/random sample/)).toBeInTheDocument();
    expect(sent).toEqual([
      {
        method: "PUT",
        url: "/api/selector",
        body: { agent_key: KEY, investigation_selector_sql: "" },
      },
    ]);
    expect(screen.queryByText(SQL)).toBeNull();
  });

  it("offers no editing outside session_review mode", async () => {
    stubFetch({ config: { ...CONFIG, quality_analysis_mode: "eval_service" } });
    renderCard();

    expect(
      await screen.findByText(
        (_, el) =>
          el?.tagName === "P" &&
          el.textContent ===
            "A selector needs session_review mode. This agent runs eval_service.",
      ),
    ).toBeInTheDocument();
    for (const name of ["Add selector", "Change selector", "Clear selector"]) {
      expect(screen.queryByRole("button", { name })).toBeNull();
    }
  });

  it("reports a failed read instead of an empty card", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(
        async () =>
          new Response(JSON.stringify({ error: "RuntimeError: exploded" })),
      ),
    );
    renderCard();

    expect(
      await screen.findByText(/Could not load the investigation selector/),
    ).toBeInTheDocument();
  });

  it("shows a failed check and hides it once the text changes", async () => {
    stubFetch({
      answerCheck: async () => reply({ error: "No agent is attached." }, 400),
    });
    renderCard();
    const user = userEvent.setup();

    await user.click(
      await screen.findByRole("button", { name: "Add selector" }),
    );
    await enterSelector(user, SQL);
    await user.click(screen.getByRole("button", { name: "Check selector" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "No agent is attached.",
    );
    expect(
      screen.getByRole("button", { name: "Save selector" }),
    ).toBeDisabled();

    await user.type(screen.getByRole("textbox", { name: "Selector" }), " ");

    expect(screen.queryByText("No agent is attached.")).toBeNull();
  });

  it("shows a failed save and keeps the editor open", async () => {
    stubFetch({
      answerSave: async () =>
        reply({ error: "Unrecognized name: target_id." }, 400),
    });
    renderCard();
    const user = userEvent.setup();

    await user.click(
      await screen.findByRole("button", { name: "Add selector" }),
    );
    await enterSelector(user, SQL);
    await user.click(screen.getByRole("button", { name: "Check selector" }));
    await screen.findByText("AQuA can run this selector");
    await user.click(screen.getByRole("button", { name: "Save selector" }));

    expect(
      await screen.findByText("Unrecognized name: target_id."),
    ).toHaveAttribute("role", "alert");
    expect(screen.getByRole("textbox", { name: "Selector" })).toHaveValue(SQL);
    expect(screen.queryByText("Saved")).toBeNull();
  });

  it("shows a failed clear in the dialog and forgets it once closed", async () => {
    stubFetch({
      config: { ...CONFIG, investigation_selector_sql: SQL },
      answerSave: async () => reply({ error: "Could not store it." }, 400),
    });
    renderCard();
    const user = userEvent.setup();

    await user.click(
      await screen.findByRole("button", { name: "Clear selector" }),
    );
    await user.click(screen.getByRole("button", { name: "Clear" }));

    const dialog = screen.getByRole("alertdialog");
    expect(await within(dialog).findByRole("alert")).toHaveTextContent(
      "Could not store it.",
    );

    await user.click(
      within(dialog).getByRole("button", { name: "Keep selector" }),
    );
    await user.click(screen.getByRole("button", { name: "Clear selector" }));

    expect(
      within(screen.getByRole("alertdialog")).queryByRole("alert"),
    ).toBeNull();
    expect(screen.getByText(SQL)).toBeInTheDocument();
  });

  it("says nothing was saved when the agent wrote nothing", async () => {
    stubFetch({
      answerSave: async () => reply({ written: false, changes: [] }),
    });
    renderCard();
    const user = userEvent.setup();

    await user.click(
      await screen.findByRole("button", { name: "Add selector" }),
    );
    await enterSelector(user, SQL);
    await user.click(screen.getByRole("button", { name: "Check selector" }));
    await screen.findByText("AQuA can run this selector");
    await user.click(screen.getByRole("button", { name: "Save selector" }));

    await waitFor(() => expect(screen.queryByRole("textbox")).toBeNull());
    expect(screen.queryByText("Saved")).toBeNull();
  });

  it("ignores a check that answers after cancel", async () => {
    let answer: (r: Response) => void = () => {};
    stubFetch({
      answerCheck: () =>
        new Promise<Response>((resolve) => {
          answer = resolve;
        }),
    });
    renderCard();
    const user = userEvent.setup();

    await user.click(
      await screen.findByRole("button", { name: "Add selector" }),
    );
    await enterSelector(user, SQL);
    await user.click(screen.getByRole("button", { name: "Check selector" }));
    await user.click(screen.getByRole("button", { name: "Cancel" }));
    await act(async () => {
      answer(reply(buildCheck()));
    });
    await user.click(screen.getByRole("button", { name: "Add selector" }));
    await enterSelector(user, SQL);

    expect(screen.queryByText("AQuA can run this selector")).toBeNull();
    expect(
      screen.getByRole("button", { name: "Save selector" }),
    ).toBeDisabled();
  });
});
