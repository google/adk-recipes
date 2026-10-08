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

import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";
import {
  createMemoryHistory,
  createRootRoute,
  createRoute,
  createRouter,
  RouterProvider,
} from "@tanstack/react-router";
import type { InsightOccurrence, InsightView } from "@/lib/aqua-api";
import { formatBugReport, Occurrence } from "../insight-detail";

function occurrence(over: Partial<InsightOccurrence>): InsightOccurrence {
  return {
    occurrence_id: "o",
    created_at: "2026-08-28T00:00:00Z",
    confidence: 0,
    diagnosis: "",
    label: "",
    item_count: 0,
    trace_count: 0,
    cases_checked: 0,
    evidence_case_ids: [],
    console_urls: {},
    ...over,
  };
}

function insightView(over: Partial<InsightView>): InsightView {
  return {
    insight_id: "ins-123",
    agent_name: "it_support_agent",
    label: "the final response carried no text",
    tool_name: "create_ticket",
    status: "RECURRING",
    occurrence_count: 7,
    trace_count: 134,
    impact: 400,
    last_run_at: "2026-08-28T18:35:34Z",
    diagnosis:
      "The create_ticket tool restricts priority to High, Medium, Low.",
    confidence: 1.0,
    ...over,
  };
}

describe("formatBugReport", () => {
  it("formats a complete markdown bug report with permalink and evidence", () => {
    const ins = insightView({
      insight_id: "ins-abc",
      agent_name: "support_agent",
      diagnosis: "Priority value must be uppercase.",
      refined_label: "create_ticket rejects a lowercase priority",
      label: "crashed on create_ticket",
      confidence: 1.0,
      impact: 400,
      trace_count: 134,
      occurrence_count: 7,
    });
    const occs = [
      occurrence({
        evidence_case_ids: ["case-01", "case-02"],
      }),
    ];

    const report = formatBugReport({
      insight: ins,
      occurrences: occs,
      origin: "http://localhost:8080",
    });

    expect(report).toContain(
      "# [Bug Report] create_ticket rejects a lowercase priority",
    );
    expect(report).toContain("**Observed agent:** support_agent");
    expect(report).toContain("**Status:** RECURRING");
    expect(report).toContain("**Impact:** 400");
    expect(report).toContain(
      "**Affected Trajectories:** 134 across 7 occurrences",
    );
    expect(report).toContain(
      "**Permalink:** http://localhost:8080/insights/ins-abc",
    );
    expect(report).toContain("Priority value must be uppercase.");
    expect(report).toContain("**Triage Signature:** crashed on create_ticket");
    expect(report).toContain("- `case-01`");
    expect(report).toContain("- `case-02`");
  });

  it("does not date an undiagnosed finding in the bug report", () => {
    // The report is read by someone else. Calling an issue nobody has
    // diagnosed "legacy" put a false provenance claim in a filed bug.
    const ins = insightView({
      insight_id: "ins-undiagnosed",
      agent_name: "it_support_agent",
      diagnosis: "",
      label: "stopped before completing the request",
      confidence: 0,
    });
    const occs = [occurrence({ confidence: 0, diagnosis: "" })];
    const report = formatBugReport({
      insight: ins,
      occurrences: occs,
      origin: "http://localhost:8080",
    });

    expect(report).toContain(
      "# [Bug Report] stopped before completing the request",
    );
    expect(report).toContain(
      "No root cause recorded: the judge model's observation, not a diagnosis.",
    );
    expect(report).not.toContain("Legacy");
  });

  it("titles the report with the label when no rename was proposed", () => {
    // The verification pass returns an empty `proposed_label` whenever the
    // stored label already names the defect, so this is the ordinary case, not
    // an edge. The diagnosis is 30-70 words and belongs under ## Diagnosis.
    const diagnosis =
      "The create_ticket tool description instructs the agent to provide a " +
      "title and priority level, but its declared parameter list is empty.";
    const report = formatBugReport({
      insight: insightView({
        refined_label: "",
        diagnosis,
        label: "create_ticket called with unpermitted arguments",
      }),
      occurrences: [occurrence({})],
    });

    expect(report).toContain(
      "# [Bug Report] create_ticket called with unpermitted arguments",
    );
    expect(report).not.toContain(`# [Bug Report] ${diagnosis}`);
    // Nothing to disambiguate: the title is already the triage signature.
    expect(report).not.toContain("**Triage Signature:**");
  });
});

describe("Occurrence", () => {
  /** Render one occurrence with only the routes it can reach. */
  function renderOccurrence(
    occ: ReturnType<typeof occurrence>,
    heading?: string,
  ) {
    const rootRoute = createRootRoute();
    const indexRoute = createRoute({
      getParentRoute: () => rootRoute,
      path: "/",
      component: () => (
        <Occurrence occurrence={occ} runId="run-777" heading={heading} />
      ),
    });
    const runRoute = createRoute({
      getParentRoute: () => rootRoute,
      path: "/investigations/$runId",
      component: () => <div>Run</div>,
    });
    const router = createRouter({
      routeTree: rootRoute.addChildren([indexRoute, runRoute]),
      history: createMemoryHistory({ initialEntries: ["/"] }),
    });
    render(<RouterProvider router={router} />);
  }

  it("links a failing conversation out to Cloud Trace", async () => {
    const occ = occurrence({
      occurrence_id: "occ-1",
      confidence: 0.9,
      diagnosis: "Tool parameter mismatch.",
      cases_checked: 5,
      evidence_case_ids: ["case-0123456789ab", "case-abcdef012345"],
      console_urls: {
        "case-0123456789ab":
          "https://console.cloud.google.com/traces/list?project=p&tid=t1",
        "case-abcdef012345":
          "https://console.cloud.google.com/traces/list?project=p&tid=t2",
      },
    });

    renderOccurrence(occ, "Latest occurrence");

    expect(await screen.findByText("Latest occurrence")).toBeInTheDocument();
    expect(await screen.findByText("Trajectories")).toBeInTheDocument();
    const links = await screen.findAllByRole("link");
    const hrefs = links.map((l) => l.getAttribute("href"));
    expect(hrefs).toContain(
      "https://console.cloud.google.com/traces/list?project=p&tid=t1",
    );
    expect(hrefs).toContain(
      "https://console.cloud.google.com/traces/list?project=p&tid=t2",
    );
  });

  it("opens the trace in a new tab, since it leaves the dashboard", async () => {
    const occ = occurrence({
      evidence_case_ids: ["case-0123456789ab"],
      console_urls: {
        "case-0123456789ab": "https://console.cloud.google.com/x",
      },
    });

    renderOccurrence(occ);

    const trace = (await screen.findAllByRole("link")).find((l) =>
      l.getAttribute("href")?.startsWith("https://console.cloud.google.com"),
    );
    expect(trace).toHaveAttribute("target", "_blank");
    expect(trace).toHaveAttribute("rel", "noreferrer");
  });

  it("offers no trace link for a conversation that has no trace ids", async () => {
    // Every `big_query` trajectory: the conversation is still replayable, so
    // the chip stays a link -- there is just no raw trace to leave for.
    const occ = occurrence({
      run_id: "run-7",
      evidence_case_ids: ["case-0123456789ab"],
    });

    renderOccurrence(occ);

    const hrefs = (await screen.findAllByRole("link")).map((l) =>
      l.getAttribute("href"),
    );
    expect(hrefs).toContain("/investigations/run-7/cases/case-0123456789ab");
    expect(hrefs.some((h) => h?.includes("console.cloud.google.com"))).toBe(
      false,
    );
  });

  it("links a case to its conversation replay", async () => {
    // The inverse of what this asserted while the endpoint 404'd. Now that
    // `/api/investigations/{run}/cases/{case}` serves, the chip is the way in.
    const occ = occurrence({
      run_id: "run-7",
      evidence_case_ids: ["case-0123456789ab"],
      console_urls: {
        "case-0123456789ab": "https://console.cloud.google.com/x",
      },
    });

    renderOccurrence(occ);

    const hrefs = (await screen.findAllByRole("link")).map((l) =>
      l.getAttribute("href"),
    );
    expect(hrefs).toContain("/investigations/run-7/cases/case-0123456789ab");
  });

  it("shows a plain chip when the sighting records no run", async () => {
    // The replay lives under a run. Without one there is no URL to build, and
    // an id with nowhere to go is still worth showing.
    const occ = occurrence({ evidence_case_ids: ["case-0123456789ab"] });

    renderOccurrence(occ);

    expect(await screen.findByText("case-0123456")).toBeInTheDocument();
    expect(screen.queryByRole("link")).toBeNull();
  });
});

describe("Occurrence -> its investigation", () => {
  it("links to the run that produced the finding", async () => {
    const occ = occurrence({ run_id: "run-777", evidence_case_ids: [] });

    const rootRoute = createRootRoute();
    const indexRoute = createRoute({
      getParentRoute: () => rootRoute,
      path: "/",
      component: () => <Occurrence occurrence={occ} runId="run-777" />,
    });
    const runRoute = createRoute({
      getParentRoute: () => rootRoute,
      path: "/investigations/$runId",
      component: () => <div>Run Page</div>,
    });
    const router = createRouter({
      routeTree: rootRoute.addChildren([indexRoute, runRoute]),
      history: createMemoryHistory({ initialEntries: ["/"] }),
    });

    render(<RouterProvider router={router} />);

    const link = await screen.findByRole("link");
    expect(link).toHaveAttribute("href", "/investigations/run-777");
    // The id has to be visible, not just in the href: an insight seen across
    // several runs shows one of these rows per run.
    expect(link).toHaveTextContent("run-777");
  });

  it("shows no investigation link when the occurrence has no run", async () => {
    const occ = occurrence({ evidence_case_ids: [] });
    delete (occ as { run_id?: string }).run_id;

    const rootRoute = createRootRoute();
    const indexRoute = createRoute({
      getParentRoute: () => rootRoute,
      path: "/",
      component: () => <Occurrence occurrence={occ} runId="unused" />,
    });
    const router = createRouter({
      routeTree: rootRoute.addChildren([indexRoute]),
      history: createMemoryHistory({ initialEntries: ["/"] }),
    });

    render(<RouterProvider router={router} />);

    expect(await screen.findByText(/Undiagnosed|%/)).toBeInTheDocument();
    expect(screen.queryByRole("link")).toBeNull();
  });
});

describe("a finding recorded from chat", () => {
  function renderOcc(occ: ReturnType<typeof occurrence>, runId: string) {
    const rootRoute = createRootRoute();
    const indexRoute = createRoute({
      getParentRoute: () => rootRoute,
      path: "/",
      component: () => <Occurrence occurrence={occ} runId={runId} />,
    });
    const chatRoute = createRoute({
      getParentRoute: () => rootRoute,
      path: "/c",
      validateSearch: (search: Record<string, unknown>) => ({
        id: search.id as string,
      }),
      component: () => <div>Chat</div>,
    });
    const runRoute = createRoute({
      getParentRoute: () => rootRoute,
      path: "/investigations/$runId",
      component: () => <div>Run</div>,
    });
    const caseRoute = createRoute({
      getParentRoute: () => rootRoute,
      path: "/investigations/$runId/cases/$caseId",
      component: () => <div>Case</div>,
    });
    const router = createRouter({
      routeTree: rootRoute.addChildren([
        indexRoute,
        chatRoute,
        runRoute,
        caseRoute,
      ]),
      history: createMemoryHistory({ initialEntries: ["/"] }),
    });
    render(<RouterProvider router={router} />);
  }

  it("links to the conversation, not an investigation that never ran", async () => {
    const occ = occurrence({ run_id: "chat-ctx-abc", evidence_case_ids: [] });

    renderOcc(occ, "chat-ctx-abc");

    const link = await screen.findByRole("link");
    expect(link).toHaveTextContent(/Open the chat/i);
    expect(link.getAttribute("href")).toContain("ctx-abc");
    expect(link.getAttribute("href")).not.toContain("/investigations/");
  });

  it("does not link when the conversation was never identified", async () => {
    // `chat-unattributed` is what a finding carries when the tool had no
    // session, and what the eight backfilled rows carry because their real
    // ids were overwritten. A link here goes to /c?id=unattributed.
    const occ = occurrence({
      run_id: "chat-unattributed",
      evidence_case_ids: [],
    });

    renderOcc(occ, "chat-unattributed");

    expect(await screen.findByText(/Recorded in chat/i)).toBeInTheDocument();
    expect(screen.queryByRole("link")).toBeNull();
  });

  it("opens a chat finding's evidence in Cloud Trace like any other", async () => {
    // The conversation is the observed agent's whichever sweep found it, and
    // a trace link needs no run at all -- which is why dropping the
    // artifact-bearing-run fallback costs this case nothing.
    const occ = occurrence({
      run_id: "chat-ctx-abc",
      evidence_case_ids: ["case-0123456789ab"],
      console_urls: {
        "case-0123456789ab": "https://console.cloud.google.com/x",
      },
    });

    renderOcc(occ, "chat-ctx-abc");

    const links = await screen.findAllByRole("link");
    const hrefs = links.map((l) => l.getAttribute("href"));
    expect(hrefs).toContain("https://console.cloud.google.com/x");
  });

  it("offers the chat and the replay, but no trace, for a chat-run case", async () => {
    const occ = occurrence({
      run_id: "chat-ctx-abc",
      evidence_case_ids: ["case-0123456789ab"],
    });

    renderOcc(occ, "chat-ctx-abc");

    expect(await screen.findByText("case-0123456")).toBeInTheDocument();
    const hrefs = screen
      .getAllByRole("link")
      .map((l) => l.getAttribute("href"));
    expect(hrefs).toContain(
      "/investigations/chat-ctx-abc/cases/case-0123456789ab",
    );
    expect(hrefs.some((h) => h?.includes("console.cloud.google.com"))).toBe(
      false,
    );
    expect(
      screen
        .getAllByRole("link")
        .some((l) => /Open the chat/i.test(l.textContent ?? "")),
    ).toBe(true);
  });

  it("still links a real run to its investigation with canonical Investigation label", async () => {
    const occ = occurrence({
      run_id: "fc27a7a2",
      evidence_case_ids: ["case-0123456789ab"],
    });

    renderOcc(occ, "fc27a7a2");

    const links = await screen.findAllByRole("link");
    const hrefs = links.map((l) => l.getAttribute("href"));
    expect(hrefs).toContain("/investigations/fc27a7a2");
    expect(screen.getByText("Investigation fc27a7a2")).toBeInTheDocument();
  });
});
