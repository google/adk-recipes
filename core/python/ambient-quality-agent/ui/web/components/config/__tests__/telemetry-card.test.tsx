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
import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { GrantCommand, TelemetryCheck } from "@/lib/aqua-api";
import { TelemetryCard } from "../telemetry-card";

const KEY = "acme-prod__billing";
const SA = "aqua@aqua-host.iam.gserviceaccount.com";
const TABLE = "acme-prod.support.events";

const CONFIG = {
  agent_key: KEY,
  observed_agent_name: "billing",
  project_id: "aqua-host",
  observed_project_id: "acme-prod",
  telemetry_dataset: "agent_analytics",
  telemetry_table: "agent_events",
  telemetry_location: "US",
  telemetry_ingestion_source: "big_query",
};

const DATASET_GRANT: GrantCommand = {
  resource: "acme-prod:support",
  role: "roles/bigquery.dataViewer",
  member: `serviceAccount:${SA}`,
  checks: ["table"],
  command: "bq query --nouse_legacy_sql --project_id=acme-prod 'GRANT …'",
};

function buildCheck(overrides: Partial<TelemetryCheck> = {}): TelemetryCheck {
  return {
    service_account: SA,
    checks: [
      { name: "table", status: "passed", target: TABLE, detail: "" },
      { name: "schema", status: "passed", target: TABLE, detail: "" },
      { name: "rows", status: "passed", target: TABLE, detail: "" },
    ],
    filled: [],
    changes: [
      { field: "telemetry_table", before: "agent_events", after: "events" },
    ],
    grants: [],
    ungranted: [],
    refusal: "",
    ...overrides,
  };
}

const DENIED = buildCheck({
  checks: [
    { name: "table", status: "denied", target: TABLE, detail: "403" },
    { name: "schema", status: "skipped", target: TABLE, detail: "" },
    { name: "rows", status: "skipped", target: TABLE, detail: "" },
  ],
  grants: [DATASET_GRANT],
});

interface Sent {
  method: string;
  url: string;
  body: Record<string, unknown>;
}

/** Answers GET /api/config with `config`, each check with the next item in
 *  `checks` (repeating the last), a save with `save`, and a grant record
 *  request with the number of grants received. Records every request body. */
function stubFetch({
  config = CONFIG as Record<string, unknown>,
  checks = [buildCheck()],
  save = { status: 200, body: { written: true } as unknown },
} = {}) {
  const sent: Sent[] = [];
  const reply = (body: unknown, status = 200) =>
    new Response(JSON.stringify(body), { status });
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string, init?: RequestInit) => {
      const method = init?.method ?? "GET";
      if (method === "GET") return reply({ config });
      const body = JSON.parse(String(init?.body));
      sent.push({ method, url, body });
      if (url === "/api/telemetry/check") {
        const n = sent.filter((s) => s.url === url).length;
        return reply(checks[Math.min(n, checks.length) - 1]);
      }
      if (url === "/api/telemetry/grants") {
        return reply({ recorded: body.granted.length });
      }
      return reply(save.body, save.status);
    }),
  );
  return {
    sent,
    checksSent: () => sent.filter((s) => s.url === "/api/telemetry/check"),
  };
}

function renderCard() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: 0 } },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <TelemetryCard />
    </QueryClientProvider>,
  );
}

async function enterTable(
  user: ReturnType<typeof userEvent.setup>,
  dataset: string,
  table: string,
) {
  await user.click(await screen.findByRole("button", { name: "Change table" }));
  const datasetInput = screen.getByRole("textbox", { name: "Dataset" });
  await user.clear(datasetInput);
  await user.type(datasetInput, dataset);
  const tableInput = screen.getByRole("textbox", { name: "Table" });
  await user.clear(tableInput);
  await user.type(tableInput, table);
}

describe("TelemetryCard", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it("shows the table the agent reads, in the agent's project", async () => {
    stubFetch();
    renderCard();

    expect(await screen.findByText("agent_analytics")).toBeInTheDocument();
    expect(screen.getByText("agent_events")).toBeInTheDocument();
    expect(screen.getByText("acme-prod")).toBeInTheDocument();
    expect(screen.getByText("US")).toBeInTheDocument();
    expect(screen.getByText("big_query")).toBeInTheDocument();
    // The edit form is hidden until the user clicks "Change table".
    expect(screen.queryByRole("textbox")).toBeNull();
    expect(screen.queryByRole("button", { name: "Save table" })).toBeNull();
    expect(
      screen.getByRole("button", { name: "Check access" }),
    ).toBeInTheDocument();
  });

  it("says why there is no table to edit", async () => {
    stubFetch({ config: { ...CONFIG, agent_key: "" } });
    renderCard();

    expect(await screen.findByText(/No agent is attached/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Change table" })).toBeNull();
  });

  it("reports a failed read instead of an empty editor", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(
        async () =>
          new Response(JSON.stringify({ error: "RuntimeError: exploded" }), {
            status: 200,
          }),
      ),
    );
    renderCard();

    expect(
      await screen.findByText(/Could not load the telemetry table/),
    ).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Change table" })).toBeNull();
  });

  it("checks the stored table's access without changing anything", async () => {
    const { checksSent } = stubFetch({
      checks: [buildCheck({ changes: [] })],
    });
    renderCard();
    const user = userEvent.setup();

    await user.click(
      await screen.findByRole("button", { name: "Check access" }),
    );

    expect(
      await screen.findByText(/AQuA already reads this table/),
    ).toBeInTheDocument();
    expect(checksSent()[0].body).toEqual({
      agent_key: KEY,
      telemetry_dataset: "agent_analytics",
      telemetry_table: "agent_events",
    });
    expect(screen.queryByRole("button", { name: "Save table" })).toBeNull();
  });

  it("checks the table in the inputs and saves it with what the table filled in", async () => {
    const changes = [
      {
        field: "telemetry_dataset",
        before: "agent_analytics",
        after: "support",
      },
      { field: "telemetry_location", before: "US", after: "EU" },
      { field: "telemetry_table", before: "agent_events", after: "events" },
    ];
    const { sent } = stubFetch({
      checks: [
        buildCheck({
          filled: [
            {
              field: "telemetry_location",
              value: "EU",
              reason: `the location of ${TABLE}`,
            },
          ],
          changes,
        }),
      ],
      save: { status: 200, body: { written: true, changes } },
    });
    renderCard();
    const user = userEvent.setup();

    await enterTable(user, " support ", "events");
    await user.click(screen.getByRole("button", { name: "Check table" }));

    expect(
      await screen.findByText("AQuA can read this table"),
    ).toBeInTheDocument();
    expect(screen.getByText("from the table")).toHaveAttribute(
      "title",
      `the location of ${TABLE}`,
    );
    expect(screen.getAllByText("ok")).toHaveLength(3);
    expect(sent[0]).toEqual({
      method: "POST",
      url: "/api/telemetry/check",
      // Inputs are trimmed; an empty bucket is omitted to keep the stored one.
      body: {
        agent_key: KEY,
        telemetry_dataset: "support",
        telemetry_table: "events",
      },
    });

    await user.click(screen.getByRole("button", { name: "Save table" }));

    expect(await screen.findByText("Saved")).toBeInTheDocument();
    expect(sent[1]).toEqual({
      method: "PUT",
      url: "/api/telemetry",
      body: {
        agent_key: KEY,
        telemetry_dataset: "support",
        telemetry_table: "events",
        telemetry_location: "EU",
      },
    });
    // After saving, the form closes and the card shows the saved values.
    expect(screen.queryByRole("textbox")).toBeNull();
    expect(screen.getByText("support")).toBeInTheDocument();
    expect(screen.getByText("EU")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Save table" })).toBeNull();
    expect(
      screen.getByText(/AQuA already reads this table/),
    ).toBeInTheDocument();
  });

  it("clears the saved changes when a check ran during the save", async () => {
    const changes = [
      {
        field: "telemetry_dataset",
        before: "agent_analytics",
        after: "support",
      },
      { field: "telemetry_table", before: "agent_events", after: "events" },
    ];
    let finishSave: (r: Response) => void = () => {};
    const checks: unknown[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url: string, init?: RequestInit) => {
        const method = init?.method ?? "GET";
        if (method === "GET") {
          return new Response(JSON.stringify({ config: CONFIG }));
        }
        if (url === "/api/telemetry/check") {
          checks.push(JSON.parse(String(init?.body)));
          return new Response(JSON.stringify(buildCheck({ changes })));
        }
        return new Promise<Response>((resolve) => {
          finishSave = resolve;
        });
      }),
    );
    renderCard();
    const user = userEvent.setup();

    await enterTable(user, "support", "events");
    await user.click(screen.getByRole("button", { name: "Check table" }));
    const save = screen.getByRole("button", { name: "Save table" });
    await waitFor(() => expect(save).toBeEnabled());
    await user.click(save);
    await user.click(screen.getByRole("button", { name: "Check again" }));
    await waitFor(() => expect(checks).toHaveLength(2));
    await act(async () => {
      finishSave(
        new Response(JSON.stringify({ written: true, changes }), {
          status: 200,
        }),
      );
    });

    expect(await screen.findByText("Saved")).toBeInTheDocument();
    expect(screen.queryByText("Changes on save")).toBeNull();
  });

  it("will not save a table an investigation cannot read", async () => {
    stubFetch({
      checks: [
        buildCheck({
          checks: [
            { name: "table", status: "missing", target: TABLE, detail: "404" },
          ],
          refusal: `${TABLE} does not exist.`,
        }),
      ],
    });
    renderCard();
    const user = userEvent.setup();

    await enterTable(user, "support", "events");
    await user.click(screen.getByRole("button", { name: "Check table" }));

    expect(
      await screen.findByText("AQuA can't use this table"),
    ).toBeInTheDocument();
    expect(
      screen.getByText(
        "The table does not exist. Check the dataset and table names.",
      ),
    ).toBeInTheDocument();
    expect(screen.getByText("missing")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Save table" })).toBeDisabled();
  });

  it("asks for a new check once the table is edited", async () => {
    stubFetch();
    renderCard();
    const user = userEvent.setup();

    await enterTable(user, "support", "events");
    await user.click(screen.getByRole("button", { name: "Check table" }));
    await screen.findByRole("button", { name: "Save table" });

    await user.type(screen.getByRole("textbox", { name: "Table" }), "_v2");

    expect(screen.getByRole("button", { name: "Save table" })).toBeDisabled();
    expect(screen.getByText("Check the table to save.")).toBeInTheDocument();
    expect(screen.queryByText("AQuA can read this table")).toBeNull();
    expect(
      screen.getByRole("button", { name: "Check table" }),
    ).toBeInTheDocument();
  });

  it("says why the table AQuA already reads cannot be saved", async () => {
    stubFetch({ checks: [buildCheck({ changes: [] })] });
    renderCard();
    const user = userEvent.setup();

    await user.click(
      await screen.findByRole("button", { name: "Change table" }),
    );
    expect(screen.getByRole("button", { name: "Save table" })).toBeDisabled();
    expect(screen.getByText("Check the table to save.")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Check table" }));

    expect(
      await screen.findByText(/AQuA already reads this table/),
    ).toBeInTheDocument();
    expect(
      screen.getByText("This table is already saved."),
    ).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Save table" })).toBeDisabled();
  });

  it("puts the stored table back on cancel", async () => {
    stubFetch();
    renderCard();
    const user = userEvent.setup();

    await enterTable(user, "support", "events");
    await user.click(screen.getByRole("button", { name: "Cancel" }));

    expect(screen.queryByRole("textbox")).toBeNull();
    expect(screen.getByText("agent_analytics")).toBeInTheDocument();
    // Reopening the form resets the inputs; the unsaved edit was discarded.
    await user.click(screen.getByRole("button", { name: "Change table" }));
    expect(screen.getByRole("textbox", { name: "Dataset" })).toHaveValue(
      "agent_analytics",
    );
  });

  it("shows the agent's reason when the check is refused", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (_url: string, init?: RequestInit) =>
        init?.method === "POST"
          ? new Response(
              JSON.stringify({ error: "No agent is attached as 'x'." }),
              { status: 400 },
            )
          : new Response(JSON.stringify({ config: CONFIG }), { status: 200 }),
      ),
    );
    renderCard();
    const user = userEvent.setup();

    await user.click(
      await screen.findByRole("button", { name: "Check access" }),
    );

    expect(
      await screen.findByText("No agent is attached as 'x'."),
    ).toBeInTheDocument();
  });

  it("re-checks a denied read and records its grant once it is in effect", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const { sent, checksSent } = stubFetch({ checks: [DENIED, buildCheck()] });
    renderCard();
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });

    await enterTable(user, "support", "events");
    await user.click(screen.getByRole("button", { name: "Check table" }));

    expect(await screen.findByText(DATASET_GRANT.command)).toBeInTheDocument();
    expect(screen.getByText("AQuA needs read access")).toBeInTheDocument();
    expect(screen.getByText("denied")).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "Copy the command" }),
    ).toBeInTheDocument();
    expect(screen.getByText(/Checking again in 15 s/)).toBeInTheDocument();
    // Save is allowed even while a read is denied; the grant can follow.
    expect(screen.getByRole("button", { name: "Save table" })).toBeEnabled();

    await act(async () => {
      await vi.advanceTimersByTimeAsync(15_000);
    });

    await waitFor(() =>
      expect(sent.at(-1)).toEqual({
        method: "POST",
        url: "/api/telemetry/grants",
        body: {
          agent_key: KEY,
          granted: [
            {
              resource: DATASET_GRANT.resource,
              role: DATASET_GRANT.role,
              member: DATASET_GRANT.member,
            },
          ],
        },
      }),
    );
    expect(checksSent()).toHaveLength(2);
    expect(checksSent()[1].body).toEqual(checksSent()[0].body);
    expect(
      await screen.findByText(
        /Recorded on the attachment so detaching the agent revokes access/,
      ),
    ).toBeInTheDocument();
    expect(screen.queryByText(DATASET_GRANT.command)).toBeNull();
    expect(screen.getByText("AQuA can read this table")).toBeInTheDocument();

    // No pending grants remain, so advancing the timer does not re-check.
    await act(async () => {
      await vi.advanceTimersByTimeAsync(300_000);
    });
    expect(checksSent()).toHaveLength(2);
  });

  it("stops re-checking a read that stays denied after the last wait", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const { sent, checksSent } = stubFetch({ checks: [DENIED] });
    renderCard();
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });

    await enterTable(user, "support", "events");
    await user.click(screen.getByRole("button", { name: "Check table" }));
    await screen.findByText(DATASET_GRANT.command);

    for (const [delay, total] of [
      [15_000, 2],
      [45_000, 3],
      [60_000, 4],
      [60_000, 5],
    ]) {
      await act(async () => {
        await vi.advanceTimersByTimeAsync(delay);
      });
      await waitFor(() => expect(checksSent()).toHaveLength(total));
    }
    await act(async () => {
      await vi.advanceTimersByTimeAsync(300_000);
    });

    expect(checksSent()).toHaveLength(5);
    expect(await screen.findByText(/Still denied/)).toBeInTheDocument();
    // No grant took effect, so none was recorded.
    expect(sent.some((s) => s.url === "/api/telemetry/grants")).toBe(false);
  });

  it("checks with the payload bucket of a source that reads one", async () => {
    const { checksSent } = stubFetch({
      config: { ...CONFIG, telemetry_ingestion_source: "cloud_logging" },
    });
    renderCard();
    const user = userEvent.setup();

    await user.click(
      await screen.findByRole("button", { name: "Change table" }),
    );
    await user.click(screen.getByText("Payload bucket (optional)"));
    await user.type(
      screen.getByRole("textbox", { name: "Payload bucket" }),
      "agent-logs",
    );
    await user.click(screen.getByRole("button", { name: "Check table" }));

    await waitFor(() => expect(checksSent()).toHaveLength(1));
    expect(checksSent()[0].body.telemetry_payload_bucket).toBe("agent-logs");
  });

  it("offers no payload bucket for a source that reads none", async () => {
    stubFetch();
    renderCard();
    const user = userEvent.setup();

    await user.click(
      await screen.findByRole("button", { name: "Change table" }),
    );

    expect(screen.queryByText("Payload bucket (optional)")).toBeNull();
  });
});
