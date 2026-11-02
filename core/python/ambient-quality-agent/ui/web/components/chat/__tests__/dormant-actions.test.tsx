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
import userEvent from "@testing-library/user-event";
import { NowProvider } from "@/lib/now-context";
import {
  DormantActions,
  DormantActionsGate,
  isDormant,
  DORMANT_THRESHOLD_MS,
} from "@/components/chat/dormant-actions";

describe("isDormant", () => {
  it("is false when last activity is within the threshold", () => {
    const now = 1_000_000_000_000;
    expect(isDormant(now - (DORMANT_THRESHOLD_MS - 1), now)).toBe(false);
  });
  it("is true once the threshold is exceeded", () => {
    const now = 1_000_000_000_000;
    expect(isDormant(now - (DORMANT_THRESHOLD_MS + 1), now)).toBe(true);
  });
  it("is false when there is no last-activity timestamp", () => {
    expect(isDormant(null, Date.now())).toBe(false);
    expect(isDormant(undefined, Date.now())).toBe(false);
  });
});

describe("DormantActions", () => {
  it("renders the three quick actions", () => {
    render(<DormantActions onAction={() => {}} />);
    expect(
      screen.getByRole("button", { name: /status check/i }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: /continue next step/i }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: /remind me the plan/i }),
    ).toBeInTheDocument();
  });

  it("sends the status-check prompt when that button is clicked", async () => {
    const user = userEvent.setup();
    const onAction = vi.fn();
    render(<DormantActions onAction={onAction} />);
    await user.click(screen.getByRole("button", { name: /status check/i }));
    expect(onAction).toHaveBeenCalledTimes(1);
    expect(onAction.mock.calls[0][0]).toMatch(/status check/i);
  });

  it("sends the continue prompt when that button is clicked", async () => {
    const user = userEvent.setup();
    const onAction = vi.fn();
    render(<DormantActions onAction={onAction} />);
    await user.click(
      screen.getByRole("button", { name: /continue next step/i }),
    );
    expect(onAction).toHaveBeenCalledWith("Continue with the next step.");
  });

  it("disables the buttons when disabled", () => {
    render(<DormantActions onAction={() => {}} disabled />);
    expect(
      screen.getByRole("button", { name: /status check/i }),
    ).toBeDisabled();
  });
});

// Exercises the REAL DormantActionsGate wired into chat-shell: its useNow()
// reads NowProvider's value, which initializes to Date.now() — so dormancy is
// computed off the live clock, and timestamps are taken relative to it.
const DORMANT_LAST_UPDATED = Date.now() - (DORMANT_THRESHOLD_MS + 60_000);
const FRESH_LAST_UPDATED = Date.now() - (DORMANT_THRESHOLD_MS - 60_000);

function renderGate(props: {
  lastUpdated: number | null;
  show: boolean;
  disabled?: boolean;
  onAction: (p: string) => void;
}) {
  return render(
    <NowProvider>
      <DormantActionsGate {...props} />
    </NowProvider>,
  );
}

describe("DormantActionsGate", () => {
  it("renders the actions when show is true and the session is dormant", () => {
    renderGate({
      lastUpdated: DORMANT_LAST_UPDATED,
      show: true,
      onAction: () => {},
    });
    expect(
      screen.getByRole("button", { name: /status check/i }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: /continue next step/i }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: /remind me the plan/i }),
    ).toBeInTheDocument();
  });

  it("renders nothing when show is false (even if dormant)", () => {
    renderGate({
      lastUpdated: DORMANT_LAST_UPDATED,
      show: false,
      onAction: () => {},
    });
    expect(
      screen.queryByRole("button", { name: /status check/i }),
    ).not.toBeInTheDocument();
  });

  it("renders nothing when the session is not dormant", () => {
    renderGate({
      lastUpdated: FRESH_LAST_UPDATED,
      show: true,
      onAction: () => {},
    });
    expect(
      screen.queryByRole("button", { name: /status check/i }),
    ).not.toBeInTheDocument();
  });

  it("renders nothing when there is no last-activity timestamp", () => {
    renderGate({ lastUpdated: null, show: true, onAction: () => {} });
    expect(
      screen.queryByRole("button", { name: /status check/i }),
    ).not.toBeInTheDocument();
  });

  it("calls onAction with the prompt when a quick action is clicked", async () => {
    const user = userEvent.setup();
    const onAction = vi.fn();
    renderGate({ lastUpdated: DORMANT_LAST_UPDATED, show: true, onAction });
    await user.click(
      screen.getByRole("button", { name: /continue next step/i }),
    );
    expect(onAction).toHaveBeenCalledWith("Continue with the next step.");
  });

  it("forwards disabled to the rendered actions", () => {
    renderGate({
      lastUpdated: DORMANT_LAST_UPDATED,
      show: true,
      disabled: true,
      onAction: () => {},
    });
    expect(
      screen.getByRole("button", { name: /status check/i }),
    ).toBeDisabled();
  });
});
