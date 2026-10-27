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
import { FailureNote } from "../failure-note";

/** Verbatim from `ui/app.py`'s `_NOT_DEPLOYED`. */
const NOT_DEPLOYED =
  "AQuA's agent is not serving its API yet. The Agent Runtime agent is still " +
  "running the placeholder image Terraform creates it with, or is rolling out " +
  "a new revision; either takes several minutes.";

const deploying = () => new Error(NOT_DEPLOYED);

describe("FailureNote", () => {
  it("draws an unfinished deployment amber, not red", () => {
    // Every pane on the page fails together for these few minutes. Red alerts
    // across all of them say the product is broken; it is still arriving.
    render(<FailureNote lead="Couldn't load the totals" error={deploying()} />);

    const note = screen.getByRole("status");
    expect(note.className).toMatch(/amber/);
    expect(note.className).not.toMatch(/destructive/);
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("drops the lead and keeps the explanation", () => {
    render(<FailureNote lead="Couldn't load the totals" error={deploying()} />);

    expect(screen.queryByText(/Couldn't load the totals/)).toBeNull();
    expect(
      screen.getByText("AQuA's agent is not serving its API yet."),
    ).toBeInTheDocument();
    expect(screen.getByText(/placeholder image/)).toBeInTheDocument();
  });

  it("states it in one line where the pane has no room", () => {
    render(
      <FailureNote
        lead="Couldn't load the chart"
        error={deploying()}
        compact
      />,
    );

    expect(
      screen.getByText("AQuA's agent is not serving its API yet."),
    ).toBeInTheDocument();
    expect(screen.queryByText(/placeholder image/)).toBeNull();
  });

  it("still alerts in red on a real fault", () => {
    render(
      <FailureNote
        lead="Couldn't load the totals"
        error={new Error("permission denied")}
      />,
    );

    const note = screen.getByRole("alert");
    expect(note.className).toMatch(/destructive/);
    expect(
      screen.getByText(/Couldn't load the totals: permission denied/),
    ).toBeInTheDocument();
  });
});
