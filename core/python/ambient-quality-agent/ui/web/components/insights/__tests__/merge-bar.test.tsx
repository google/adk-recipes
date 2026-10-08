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

import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { InsightView } from "@/lib/aqua-api";
import { MergeBar } from "../merge-bar";

function view(overrides: Partial<InsightView>): InsightView {
  return {
    insight_id: "ins-1",
    agent_name: "a",
    label: "x",
    tool_name: "",
    status: "RECURRING",
    occurrence_count: 0,
    trace_count: 0,
    last_run_at: null,
    impact: 0,
    diagnosis: "",
    confidence: 0,
    ...overrides,
  };
}

const A = view({
  insight_id: "a",
  label: "signature A",
  diagnosis: "the real root cause",
});
const B = view({ insight_id: "b", label: "signature B" });

describe("MergeBar", () => {
  it("offers no survivor button with fewer than two selected", () => {
    render(
      <MergeBar
        insights={[A, B]}
        selected={new Set(["a"])}
        onCancel={() => {}}
        onMerge={() => {}}
        merging={false}
      />,
    );
    const button = screen.getByRole("button", {
      name: /Keep: the real root cause/,
    });
    expect(button).toBeDisabled();
    expect(
      screen.getByText(
        /Select two or more cards that represent the same underlying insight/,
      ),
    ).toBeInTheDocument();
  });

  it("calls onMerge with the chosen survivor, not the first selected by default", async () => {
    const onMerge = vi.fn();
    render(
      <MergeBar
        insights={[A, B]}
        selected={new Set(["a", "b"])}
        onCancel={() => {}}
        onMerge={onMerge}
        merging={false}
      />,
    );

    const pickA = screen.getByRole("button", {
      name: /Keep: the real root cause/,
    });
    expect(pickA).not.toBeDisabled();
    await userEvent.click(pickA);

    expect(onMerge).toHaveBeenCalledWith("a");
  });

  it("calls onCancel and disables actions while merging", () => {
    const onCancel = vi.fn();
    render(
      <MergeBar
        insights={[A, B]}
        selected={new Set(["a", "b"])}
        onCancel={onCancel}
        onMerge={() => {}}
        merging={true}
      />,
    );

    const cancel = screen.getByRole("button", { name: "Cancel" });
    expect(cancel).toBeDisabled();
    const pickA = screen.getByRole("button", {
      name: /Keep: the real root cause/,
    });
    expect(pickA).toBeDisabled();
  });
});
