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
import type { RootCause } from "@/lib/aqua-api";
import {
  RootCauseRecord,
  RootCauseToolResult,
  newestRootCause,
  rootCausePreview,
} from "../root-cause-record";

function record(over: Partial<RootCause> = {}): RootCause {
  return {
    root_cause_id: "rc-1",
    insight_id: "ins-1",
    occurrence_id: "occ-1",
    agent_revision: "rev-9",
    summary: "The refund step is phrased as a preference, not a constraint.",
    edits: [
      {
        path: "app/agent.py",
        start_line: 42,
        end_line: 44,
        before: "old line",
        after: "new line",
        rationale: "Make the step mandatory.",
      },
    ],
    created_at: "2026-01-02T03:04:05Z",
    ...over,
  };
}

describe("RootCauseRecord", () => {
  it("renders the summary and one before/after block per edit", () => {
    const { container } = render(<RootCauseRecord record={record()} />);

    expect(screen.getByText(/phrased as a preference/)).toBeInTheDocument();
    expect(screen.getByText("app/agent.py:42-44")).toBeInTheDocument();
    expect(screen.getByText("Make the step mandatory.")).toBeInTheDocument();

    const lines = Array.from(container.querySelectorAll("pre > div")).map(
      (d) => d.textContent ?? "",
    );
    expect(lines).toContain("-old line");
    expect(lines).toContain("+new line");
  });

  it("omits the rationale line when the edit carries none", () => {
    render(
      <RootCauseRecord
        record={record({
          edits: [
            {
              path: "app/agent.py",
              start_line: 1,
              end_line: 1,
              before: "a",
              after: "b",
            },
          ],
        })}
      />,
    );
    expect(screen.getByText("app/agent.py:1-1")).toBeInTheDocument();
    expect(screen.queryByText(/Make the step mandatory/)).toBeNull();
  });

  it("says so when the diagnosis proposes no code change", () => {
    render(<RootCauseRecord record={record({ edits: [] })} />);
    expect(screen.getByText("No code changes proposed.")).toBeInTheDocument();
  });

  it("surfaces warnings alongside the record", () => {
    render(
      <RootCauseRecord
        record={record()}
        warnings={["The edits are anchored at revision 'rev-9'."]}
      />,
    );
    expect(
      screen.getByText(/anchored at revision 'rev-9'/),
    ).toBeInTheDocument();
  });

  it("shows the note when only the newest of several records is drawn", () => {
    render(
      <RootCauseRecord
        record={record()}
        note="Showing the latest of 3 records."
      />,
    );
    expect(
      screen.getByText("Showing the latest of 3 records."),
    ).toBeInTheDocument();
  });
});

describe("newestRootCause", () => {
  it("picks the most recently recorded of a set", () => {
    const older = record({
      root_cause_id: "old",
      created_at: "2026-01-01T00:00:00Z",
    });
    const newer = record({
      root_cause_id: "new",
      created_at: "2026-03-01T00:00:00Z",
    });
    expect(newestRootCause([older, newer])?.root_cause_id).toBe("new");
    expect(newestRootCause([newer, older])?.root_cause_id).toBe("new");
  });

  it("is undefined when there is nothing to pick", () => {
    expect(newestRootCause([])).toBeUndefined();
  });
});

describe("rootCausePreview", () => {
  it("counts the edits and names the single file they touch", () => {
    expect(
      rootCausePreview({
        edits: [
          { path: "app/agent.py", start_line: 1, end_line: 2 },
          { path: "app/agent.py", start_line: 8, end_line: 9 },
        ],
      }),
    ).toBe("2 edits · app/agent.py");
  });

  it("counts the files instead when the edits span several", () => {
    expect(
      rootCausePreview({
        edits: [
          { path: "app/agent.py", start_line: 1, end_line: 2 },
          { path: "app/tools.py", start_line: 3, end_line: 4 },
        ],
      }),
    ).toBe("2 edits · 2 files");
  });

  it("reads 'no edits' for a diagnosis that proposes none", () => {
    expect(rootCausePreview({ edits: [] })).toBe("no edits");
    expect(rootCausePreview(null)).toBe("no edits");
  });
});

describe("RootCauseToolResult", () => {
  it("waits visibly while the call is still running", () => {
    render(<RootCauseToolResult result={null} hasResult={false} />);
    expect(screen.getByText(/recording the root cause…/)).toBeInTheDocument();
  });

  it("renders the record the server stored, not the arguments sent", () => {
    render(
      <RootCauseToolResult
        result={{ recorded: true, record: record(), warnings: [] }}
        hasResult
      />,
    );
    expect(screen.getByTestId("root-cause-record")).toBeInTheDocument();
  });

  it("names every refusal when recording was rejected wholesale", () => {
    render(
      <RootCauseToolResult
        result={{
          recorded: false,
          anchored: [
            {
              path: "app/ok.py",
              start_line: 1,
              end_line: 2,
              before: "a",
              after: "b",
            },
          ],
          rejected: [
            {
              path: "app/gone.py",
              start_line: 10,
              end_line: 12,
              reason: "app/gone.py is not in the snapshot.",
            },
          ],
        }}
        hasResult
      />,
    );
    expect(screen.queryByTestId("root-cause-record")).toBeNull();
    expect(screen.getByText("app/gone.py:10-12")).toBeInTheDocument();
    expect(
      screen.getByText("app/gone.py is not in the snapshot."),
    ).toBeInTheDocument();
    // Verify anchored edits are reported so valid changes can be reused.
    expect(screen.getByText(/1 edit anchored cleanly/)).toBeInTheDocument();
  });

  it("shows a guard-rail error, which arrives instead of the envelope", () => {
    render(
      <RootCauseToolResult
        result={{ error: "insight_id is required" }}
        hasResult
      />,
    );
    expect(screen.getByText("insight_id is required")).toBeInTheDocument();
  });
});
