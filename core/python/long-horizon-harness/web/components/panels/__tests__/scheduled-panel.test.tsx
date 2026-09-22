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

import { describe, it, expect, vi } from "vitest";
import { render, screen } from "@testing-library/react";

vi.mock("@/components/panels/routines-panel", () => ({
  RoutinesPanel: () => <div>ROUTINES_BODY</div>,
}));

import { ScheduledPanel } from "@/components/panels/scheduled-panel";

describe("ScheduledPanel", () => {
  it("renders the routines panel", () => {
    render(<ScheduledPanel />);
    expect(screen.getByText("ROUTINES_BODY")).toBeInTheDocument();
  });
});
