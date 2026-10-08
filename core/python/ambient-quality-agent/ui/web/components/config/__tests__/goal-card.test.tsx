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

// biome-ignore-all lint/style/noNonNullAssertion: a missing value fails the test either way; the assertion only narrows the type.

import { describe, expect, it, vi, beforeEach } from "vitest";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { GoalCard } from "../goal-card";

function renderCard(fetchImpl: typeof fetch) {
  vi.stubGlobal("fetch", fetchImpl);
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: 0 } },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <GoalCard />
    </QueryClientProvider>,
  );
}

describe("GoalCard", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });

  it("shows a warning naming the missing bucket, not a blank textarea", async () => {
    renderCard((() =>
      Promise.resolve(
        new Response(
          JSON.stringify({
            goal: null,
            available: false,
            reason:
              "AQA_METRICS_GCS_BUCKET is not configured for this service.",
          }),
          { status: 200 },
        ),
      )) as typeof fetch);

    expect(
      await screen.findByText(/AQA_METRICS_GCS_BUCKET/),
    ).toBeInTheDocument();
  });

  it("renders the current goal in an editable textarea", async () => {
    renderCard((() =>
      Promise.resolve(
        new Response(
          JSON.stringify({
            goal: "Handle billing tickets.",
            available: true,
            reason: null,
          }),
          { status: 200 },
        ),
      )) as typeof fetch);

    const textarea = (await screen.findByDisplayValue(
      "Handle billing tickets.",
    )) as HTMLTextAreaElement;
    expect(textarea.tagName).toBe("TEXTAREA");
    expect(
      screen.getByText(/What AQuA should look hardest at/),
    ).toBeInTheDocument();
  });

  it("shows the id of the active version", async () => {
    renderCard((() =>
      Promise.resolve(
        new Response(
          JSON.stringify({
            goal: "Handle billing tickets.",
            available: true,
            version: "0123456789ab",
          }),
          { status: 200 },
        ),
      )) as typeof fetch);

    expect(await screen.findByText(/version 0123456789ab/)).toBeInTheDocument();
  });

  it("shows an example goal in the empty box", async () => {
    renderCard((() =>
      Promise.resolve(
        new Response(
          JSON.stringify({ goal: null, available: true, version: null }),
          { status: 200 },
        ),
      )) as typeof fetch);

    // A whole goal in the shape the review uses: what the agent is for, what
    // matters most, where to look hardest, what does not matter.
    const box = (await screen.findByPlaceholderText(
      /^Our agent answers billing questions/,
    )) as HTMLTextAreaElement;
    const lines = box.placeholder.split("\n");
    expect(lines).toHaveLength(4);
    expect(lines[1]).toMatch(/^What matters most: /);
    expect(lines[2]).toMatch(/^Look hardest at /);
    expect(lines[3]).toBe("Tone, greetings and small talk do not matter.");
  });

  it("says AQuA follows its own instructions while no goal is saved", async () => {
    renderCard((() =>
      Promise.resolve(
        new Response(
          JSON.stringify({ goal: null, available: true, version: null }),
          { status: 200 },
        ),
      )) as typeof fetch);

    expect(
      await screen.findByText(
        "No goal saved. Until you save one, AQuA uses only its own instructions.",
      ),
    ).toBeInTheDocument();
  });

  it("shows why a save was refused, not just its status", async () => {
    renderCard((async (_input: RequestInfo | URL, init?: RequestInit) => {
      if (init?.method === "PUT") {
        return new Response(
          JSON.stringify({
            error: "The goal is longer than 8 KiB; shorten it.",
          }),
          { status: 400 },
        );
      }
      return new Response(
        JSON.stringify({ goal: "Old goal.", available: true }),
        { status: 200 },
      );
    }) as typeof fetch);

    const textarea = await screen.findByDisplayValue("Old goal.");
    await userEvent.type(textarea, " more");
    await userEvent.click(screen.getByRole("button", { name: "Save goal" }));

    expect(
      await screen.findByText("The goal is longer than 8 KiB; shorten it."),
    ).toBeInTheDocument();
  });

  it("disables Save until the text changes, and calls PUT with the new text", async () => {
    const calls: { method?: string; body?: string }[] = [];
    renderCard((async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = typeof input === "string" ? input : input.toString();
      if (
        url.includes("/api/goal") &&
        (!init?.method || init.method === "GET")
      ) {
        return new Response(
          JSON.stringify({
            goal: "Old goal.",
            available: true,
            reason: null,
          }),
          { status: 200 },
        );
      }
      calls.push({ method: init?.method, body: init?.body as string });
      return new Response(JSON.stringify({ goal: "New goal." }), {
        status: 200,
      });
    }) as typeof fetch);

    await screen.findByDisplayValue("Old goal.");
    const saveButton = screen.getByRole("button", { name: "Save goal" });
    expect((saveButton as HTMLButtonElement).disabled).toBe(true);

    const textarea = screen.getByDisplayValue("Old goal.");
    await userEvent.clear(textarea);
    await userEvent.type(textarea, "New goal.");
    expect((saveButton as HTMLButtonElement).disabled).toBe(false);

    await userEvent.click(saveButton);
    expect(calls.length).toBe(1);
    expect(calls[0].method).toBe("PUT");
    expect(JSON.parse(calls[0].body!)).toEqual({
      goal: "New goal.",
      text: "New goal.",
    });
  });

  it("refuses to save a goal over 8 KiB before sending it", async () => {
    renderCard((() =>
      Promise.resolve(
        new Response(JSON.stringify({ goal: "Old goal.", available: true }), {
          status: 200,
        }),
      )) as typeof fetch);

    const textarea = await screen.findByDisplayValue("Old goal.");
    fireEvent.change(textarea, { target: { value: "x".repeat(8193) } });

    expect(
      screen.getByText(/8,193 \/ 8,192 bytes — too long to save/),
    ).toBeInTheDocument();
    expect(
      (screen.getByRole("button", { name: "Save goal" }) as HTMLButtonElement)
        .disabled,
    ).toBe(true);
  });

  it("removes the goal when the box is emptied", async () => {
    const puts: string[] = [];
    renderCard((async (_input: RequestInfo | URL, init?: RequestInit) => {
      if (init?.method === "PUT") {
        puts.push(init.body as string);
        return new Response(JSON.stringify({ goal: "", version: null }), {
          status: 200,
        });
      }
      return new Response(
        JSON.stringify({ goal: "Old goal.", available: true }),
        {
          status: 200,
        },
      );
    }) as typeof fetch);

    await userEvent.clear(await screen.findByDisplayValue("Old goal."));
    await userEvent.click(screen.getByRole("button", { name: "Remove goal" }));

    await vi.waitFor(() => expect(puts.length).toBe(1));
    expect(JSON.parse(puts[0])).toEqual({ goal: "", text: "" });
  });

  it("discards unsaved changes", async () => {
    renderCard((() =>
      Promise.resolve(
        new Response(JSON.stringify({ goal: "Old goal.", available: true }), {
          status: 200,
        }),
      )) as typeof fetch);

    await userEvent.type(await screen.findByDisplayValue("Old goal."), " more");
    await userEvent.click(
      screen.getByRole("button", { name: "Discard changes" }),
    );

    expect(screen.getByDisplayValue("Old goal.")).toBeInTheDocument();
  });
});

const SECOND = {
  version: "bbbbbbbbbbbb",
  text: "Second goal.",
  created_at: "2026-09-02T09:00:00+00:00",
  last_activated_at: "2026-09-02T09:00:00+00:00",
  active: true,
};
const FIRST = {
  version: "aaaaaaaaaaaa",
  text: "First goal.\nWith a second line.",
  created_at: "2026-09-01T09:00:00+00:00",
  last_activated_at: "2026-09-01T09:00:00+00:00",
  active: false,
};

/** Serves the goal and its versions, and records every PUT body. */
function withVersions(versions: object[], puts: string[] = []): typeof fetch {
  return (async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = typeof input === "string" ? input : input.toString();
    if (init?.method === "PUT") {
      puts.push(init.body as string);
      return new Response(JSON.stringify({ goal: "restored" }), {
        status: 200,
      });
    }
    if (url.endsWith("/api/goal/versions")) {
      return new Response(JSON.stringify({ versions, available: true }), {
        status: 200,
      });
    }
    return new Response(
      JSON.stringify({
        goal: "Second goal.",
        available: true,
        version: SECOND.version,
      }),
      { status: 200 },
    );
  }) as typeof fetch;
}

describe("GoalCard history", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });

  it("restores an earlier version into the editor, and saves it on Save", async () => {
    const puts: string[] = [];
    renderCard(withVersions([SECOND, FIRST], puts));

    await userEvent.click(
      await screen.findByRole("button", { name: /Previous versions \(1\)/ }),
    );
    await userEvent.click(
      screen.getByRole("button", { name: "Restore version aaaaaaaaaaaa" }),
    );

    // Nothing is saved until the developer has seen the text in the editor.
    expect(puts).toHaveLength(0);
    expect((screen.getByRole("textbox") as HTMLTextAreaElement).value).toBe(
      FIRST.text,
    );
    expect(
      screen.getByText(/Save to make it the goal again\./),
    ).toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: "Save goal" }));

    await vi.waitFor(() => expect(puts.length).toBe(1));
    expect(JSON.parse(puts[0])).toEqual({ goal: FIRST.text, text: FIRST.text });
  });

  it("lists only the versions before the saved one", async () => {
    renderCard(withVersions([SECOND, FIRST]));

    await userEvent.click(
      await screen.findByRole("button", { name: /Previous versions \(1\)/ }),
    );

    expect(
      screen.getByRole("button", { name: "Restore version aaaaaaaaaaaa" }),
    ).toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "Restore version bbbbbbbbbbbb" }),
    ).not.toBeInTheDocument();
  });

  it("does not restore over unsaved edits", async () => {
    renderCard(withVersions([SECOND, FIRST]));

    await userEvent.type(
      await screen.findByDisplayValue("Second goal."),
      " edited",
    );
    await userEvent.click(
      screen.getByRole("button", { name: /Previous versions \(1\)/ }),
    );

    expect(
      (
        screen.getByRole("button", {
          name: "Restore version aaaaaaaaaaaa",
        }) as HTMLButtonElement
      ).disabled,
    ).toBe(true);
  });

  it("shows a long version in full only when asked", async () => {
    const long = {
      ...FIRST,
      text: Array.from(
        { length: 8 },
        (_, i) => `Rule ${i + 1} of the goal.`,
      ).join("\n"),
    };
    renderCard(withVersions([SECOND, long]));

    await userEvent.click(
      await screen.findByRole("button", { name: /Previous versions \(1\)/ }),
    );
    // The long version starts clamped, behind Show all.
    expect(screen.getAllByRole("button", { name: "Show all" })).toHaveLength(1);

    await userEvent.click(screen.getByRole("button", { name: "Show all" }));

    expect(
      screen.getByRole("button", { name: "Show less" }),
    ).toBeInTheDocument();
    expect(screen.getByText(/Rule 8 of the goal\./)).toBeInTheDocument();
  });

  it("offers a removed goal for restore", async () => {
    const removed = { ...SECOND, active: false };
    renderCard((async (input: RequestInfo | URL) => {
      const url = typeof input === "string" ? input : input.toString();
      if (url.endsWith("/api/goal/versions")) {
        return new Response(
          JSON.stringify({ versions: [removed], available: true }),
          {
            status: 200,
          },
        );
      }
      return new Response(
        JSON.stringify({ goal: null, available: true, version: null }),
        {
          status: 200,
        },
      );
    }) as typeof fetch);

    await userEvent.click(
      await screen.findByRole("button", { name: /Previous versions \(1\)/ }),
    );

    expect(
      screen.getByRole("button", { name: "Restore version bbbbbbbbbbbb" }),
    ).toBeInTheDocument();
  });

  it("shows no history until there is an earlier version", async () => {
    renderCard(withVersions([SECOND]));

    await screen.findByDisplayValue("Second goal.");
    expect(
      screen.queryByRole("button", { name: /Previous versions/ }),
    ).not.toBeInTheDocument();
  });
});
