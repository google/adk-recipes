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
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { ScheduleCard } from "../schedule-card";

const ATTACHED = {
  available: true,
  agent_key: "acme-prod__billing",
  agent_name: "billing",
  investigation_schedule: "0 12 * * *",
  scheduled_trigger_enabled: true,
  updated_at: "2026-10-14T09:00:00+00:00",
};

/** Answers GET /api/schedule with `schedule`, and PUT with `put`; records
 *  every PUT body. */
function stubFetch(
  schedule: unknown,
  put: { status: number; body: unknown } = {
    status: 200,
    body: { written: true },
  },
) {
  const puts: unknown[] = [];
  const fetchImpl = vi.fn(async (_url: string, init?: RequestInit) => {
    if (init?.method === "PUT") {
      puts.push(JSON.parse(String(init.body)));
      return new Response(JSON.stringify(put.body), { status: put.status });
    }
    return new Response(JSON.stringify(schedule), { status: 200 });
  });
  vi.stubGlobal("fetch", fetchImpl);
  return { fetchImpl, puts };
}

function renderCard() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: 0 } },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <ScheduleCard />
    </QueryClientProvider>,
  );
}

describe("ScheduleCard", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
  });

  it("shows the investigated agent's cron, phrased, and its switch", async () => {
    stubFetch(ATTACHED);
    renderCard();

    expect(await screen.findByDisplayValue("0 12 * * *")).toBeInTheDocument();
    expect(screen.getByText("daily at 12:00 UTC")).toBeInTheDocument();
    expect(screen.getByText("billing")).toBeInTheDocument();
    expect(
      screen.getByRole("checkbox", { name: /investigate on this schedule/i }),
    ).toBeChecked();
    expect(
      screen.getByRole("button", { name: "Save schedule" }),
    ).toBeDisabled();
  });

  it("says why there is nothing to edit", async () => {
    stubFetch({
      available: false,
      reason: "No agent is attached. Attach one with `agents-cli aqua attach`.",
    });
    renderCard();

    expect(await screen.findByText(/No agent is attached/)).toBeInTheDocument();
    expect(screen.queryByRole("textbox")).toBeNull();
  });

  it("reports a failed read instead of an empty editor", async () => {
    stubFetch({ error: "RuntimeError: backend exploded" });
    renderCard();

    expect(
      await screen.findByText(/Could not load the schedule/),
    ).toBeInTheDocument();
    expect(screen.queryByRole("textbox")).toBeNull();
  });

  it("saves only the cron when only the cron changed", async () => {
    const { puts } = stubFetch(ATTACHED);
    renderCard();
    const user = userEvent.setup();

    const input = await screen.findByDisplayValue("0 12 * * *");
    await user.clear(input);
    await user.type(input, " 30 6 * * 1-5 ");
    await user.click(screen.getByRole("button", { name: "Save schedule" }));

    await waitFor(() => expect(puts).toHaveLength(1));
    // Trimmed: the engine stores the cron as sent.
    expect(puts[0]).toEqual({
      agent_key: "acme-prod__billing",
      investigation_schedule: "30 6 * * 1-5",
    });
    expect(await screen.findByText("Saved")).toBeInTheDocument();
  });

  it("saves only the switch when only the switch changed", async () => {
    const { puts } = stubFetch(ATTACHED);
    renderCard();
    const user = userEvent.setup();

    await user.click(
      await screen.findByRole("checkbox", {
        name: /investigate on this schedule/i,
      }),
    );
    expect(
      screen.getByText(/Scheduled investigations are off/),
    ).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Save schedule" }));

    await waitFor(() => expect(puts).toHaveLength(1));
    expect(puts[0]).toEqual({
      agent_key: "acme-prod__billing",
      scheduled_trigger_enabled: false,
    });
  });

  it("submits on Enter", async () => {
    const { puts } = stubFetch(ATTACHED);
    renderCard();
    const user = userEvent.setup();

    const input = await screen.findByDisplayValue("0 12 * * *");
    await user.clear(input);
    await user.type(input, "0 6 * * *{Enter}");

    await waitFor(() => expect(puts).toHaveLength(1));
    expect(puts[0]).toEqual({
      agent_key: "acme-prod__billing",
      investigation_schedule: "0 6 * * *",
    });
  });

  it("shows the saved cron, read back, once it says Saved", async () => {
    let stored = ATTACHED;
    const fetchImpl = vi.fn(async (_url: string, init?: RequestInit) => {
      if (init?.method === "PUT") {
        const sent = JSON.parse(String(init.body));
        stored = { ...stored, ...sent };
        return new Response(JSON.stringify({ written: true }), {
          status: 200,
        });
      }
      return new Response(JSON.stringify(stored), { status: 200 });
    });
    vi.stubGlobal("fetch", fetchImpl);
    renderCard();
    const user = userEvent.setup();

    const input = await screen.findByDisplayValue("0 12 * * *");
    await user.clear(input);
    await user.type(input, "0 6 * * *{Enter}");

    expect(await screen.findByText("Saved")).toBeInTheDocument();
    expect(screen.getByDisplayValue("0 6 * * *")).toBeInTheDocument();
    expect(screen.getByText("daily at 06:00 UTC")).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "Save schedule" }),
    ).toBeDisabled();
  });

  it("shows the agent's reason when it refuses the cron", async () => {
    stubFetch(ATTACHED, {
      status: 400,
      body: {
        error:
          "investigation_schedule must be a five-field cron expression in UTC",
      },
    });
    renderCard();
    const user = userEvent.setup();

    const input = await screen.findByDisplayValue("0 12 * * *");
    await user.clear(input);
    await user.type(input, "every day");
    await user.click(screen.getByRole("button", { name: "Save schedule" }));

    expect(
      await screen.findByText(/must be a five-field cron/),
    ).toBeInTheDocument();
    // The draft stays, so the user can fix it rather than retype it.
    expect(screen.getByDisplayValue("every day")).toBeInTheDocument();
  });

  it("will not save an empty cron", async () => {
    stubFetch(ATTACHED);
    renderCard();
    const user = userEvent.setup();

    await user.clear(await screen.findByDisplayValue("0 12 * * *"));

    expect(
      screen.getByRole("button", { name: "Save schedule" }),
    ).toBeDisabled();
  });

  it("puts the saved values back on discard", async () => {
    stubFetch(ATTACHED);
    renderCard();
    const user = userEvent.setup();

    const input = await screen.findByDisplayValue("0 12 * * *");
    await user.clear(input);
    await user.type(input, "0 6 * * *");
    await user.click(screen.getByRole("button", { name: "Discard changes" }));

    expect(screen.getByDisplayValue("0 12 * * *")).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "Save schedule" }),
    ).toBeDisabled();
  });
});
