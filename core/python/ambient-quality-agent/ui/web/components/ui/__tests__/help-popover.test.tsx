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
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import { HelpPopover } from "../help-popover";

const TEXT = "What ingestion did.";

/** The box a sighted reader sees. Hidden from assistive tech, which hears the
 * same text through the status region instead. */
const box = () => screen.queryByRole("dialog", { hidden: true });
const announced = () => screen.getByRole("status").textContent;

describe("HelpPopover", () => {
  it("opens on a tap", async () => {
    const user = userEvent.setup();
    render(<HelpPopover topic="Ingested">{TEXT}</HelpPopover>);

    expect(box()).toBeNull();
    await user.pointer({
      keys: "[TouchA]",
      target: screen.getByRole("button", { name: "About Ingested" }),
    });
    expect(
      await screen.findByRole("dialog", { hidden: true }),
    ).toHaveTextContent(TEXT);
  });

  it("opens on a click", async () => {
    const user = userEvent.setup();
    render(<HelpPopover topic="Ingested">{TEXT}</HelpPopover>);

    await user.click(screen.getByRole("button", { name: "About Ingested" }));
    expect(
      await screen.findByRole("dialog", { hidden: true }),
    ).toHaveTextContent(TEXT);
  });

  it("opens from the keyboard and closes on Escape", async () => {
    const user = userEvent.setup();
    render(<HelpPopover topic="Ingested">{TEXT}</HelpPopover>);

    await user.tab();
    expect(
      screen.getByRole("button", { name: "About Ingested" }),
    ).toHaveFocus();
    await user.keyboard("{Enter}");
    expect(
      await screen.findByRole("dialog", { hidden: true }),
    ).toHaveTextContent(TEXT);

    await user.keyboard("{Escape}");
    expect(box()).toBeNull();
  });

  // The text holds nothing to focus, so moving focus into it would leave Tab
  // with nowhere to go.
  it("keeps focus on the button when the keyboard opens it", async () => {
    const user = userEvent.setup();
    render(
      <>
        <HelpPopover topic="Ingested">{TEXT}</HelpPopover>
        <button type="button">Next</button>
      </>,
    );
    const trigger = screen.getByRole("button", { name: "About Ingested" });

    await user.tab();
    await user.keyboard("{Enter}");
    await screen.findByRole("dialog", { hidden: true });
    expect(trigger).toHaveFocus();

    await user.tab();
    expect(screen.getByRole("button", { name: "Next" })).toHaveFocus();
    expect(box()).toBeNull();
  });

  it("returns focus to the button when Escape closes it", async () => {
    const user = userEvent.setup();
    render(<HelpPopover topic="Ingested">{TEXT}</HelpPopover>);
    const trigger = screen.getByRole("button", { name: "About Ingested" });

    await user.click(trigger);
    await screen.findByRole("dialog", { hidden: true });
    await user.keyboard("{Escape}");
    expect(box()).toBeNull();
    expect(trigger).toHaveFocus();
  });

  // The status region is `sr-only`, an absolute 1px box. With no positioned
  // parent it is placed against the page, and one beside a chart far down Home
  // made the whole document scroll. jsdom does no layout, so this pins the
  // parent that contains it rather than the page height.
  it("keeps its status region inside a positioned wrapper", () => {
    render(<HelpPopover topic="Ingested">{TEXT}</HelpPopover>);
    const wrapper = screen.getByRole("status").parentElement;
    expect(wrapper).toHaveClass("relative");
    expect(wrapper).toContainElement(
      screen.getByRole("button", { name: "About Ingested" }),
    );
  });

  // Focus stays on the button, so a screen reader hears the text only if it
  // is announced: the status region is in the page before it opens, and is
  // filled on open, which is what makes a live region speak.
  it("announces the text when it opens, and clears it when it closes", async () => {
    const user = userEvent.setup();
    render(
      <>
        <HelpPopover topic="Ingested">{TEXT}</HelpPopover>
        <button type="button">Next</button>
      </>,
    );
    expect(announced()).toBe("");

    await user.tab();
    await user.keyboard("{Enter}");
    await screen.findByRole("dialog", { hidden: true });
    expect(announced()).toBe(TEXT);

    await user.keyboard("{Escape}");
    expect(announced()).toBe("");

    await user.keyboard("{Enter}");
    await screen.findByRole("dialog", { hidden: true });
    await user.tab();
    expect(announced()).toBe("");
  });

  it("reads the text once, through the announcement", async () => {
    const user = userEvent.setup();
    render(<HelpPopover topic="Ingested">{TEXT}</HelpPopover>);

    await user.click(screen.getByRole("button", { name: "About Ingested" }));
    const dialog = await screen.findByRole("dialog", { hidden: true });
    expect(dialog).toHaveAttribute("aria-hidden", "true");
    expect(dialog).toHaveAttribute("aria-label", "About Ingested");
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("has a focus ring an overflow-hidden parent cannot clip", () => {
    render(<HelpPopover topic="Ingested">{TEXT}</HelpPopover>);
    expect(screen.getByRole("button", { name: "About Ingested" })).toHaveClass(
      "focus-visible:ring-2",
      "focus-visible:ring-inset",
    );
  });
});
