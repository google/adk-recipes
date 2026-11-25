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

import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { PickedFor } from "../picked-for";

const none = {
  has_error: false,
  has_abnormal_finish: false,
  has_tool_reported_error: false,
  busy_turn_tail: 0,
  cost_tail: 0,
  latency_tail: 0,
};

describe("PickedFor", () => {
  it("shows a chip per signal that made the trajectory likelier to be picked", () => {
    render(
      <PickedFor
        signals={{
          ...none,
          has_error: true,
          cost_percentile: 0.95,
          cost_tail: 0.5,
        }}
        probability={0.25}
      />,
    );

    const line = screen.getByTestId("picked-for");
    expect(line).toHaveTextContent("Prioritized for");
    const chips = screen.getAllByRole("listitem");
    expect(chips.map((chip) => chip.getAttribute("aria-label"))).toEqual([
      "Failed call",
      "High token usage (top 5%)",
    ]);
    expect(chips[1]).toHaveAttribute(
      "title",
      expect.stringMatching(/This conversation: top 5%\.$/),
    );
    expect(screen.getByText("Prioritized for")).toHaveAttribute(
      "title",
      expect.stringMatching(/was 25%/),
    );
  });

  it("does not round a small chance down to nothing", () => {
    render(
      <PickedFor signals={{ ...none, has_error: true }} probability={0.004} />,
    );

    expect(screen.getByText("Prioritized for")).toHaveAttribute(
      "title",
      expect.stringMatching(/was under 1%/),
    );
  });

  it("says nothing for a trajectory no signal raised", () => {
    const { container } = render(<PickedFor signals={none} />);

    expect(container).toBeEmptyDOMElement();
  });
});
