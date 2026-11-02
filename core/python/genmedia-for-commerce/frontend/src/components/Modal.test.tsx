/**
 * Copyright 2026 Google LLC
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     https://www.apache.org/licenses/LICENSE-2.0
 *
 * Unless required by applicable law or agreed to in writing, software
 * distributed under the License is distributed on an "AS IS" BASIS,
 * WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 * See the License for the specific language governing permissions and
 * limitations under the License.
 */

import { fireEvent, render, screen } from "@testing-library/react";
import { useState } from "react";
import { describe, expect, it } from "vitest";
import ImageLightbox from "./ImageLightbox";
import Modal from "./Modal";

/** A modal opened from a button inside a blurred card, as the pickers are. */
function Opener({ autoFocus = false }: { autoFocus?: boolean }) {
  const [open, setOpen] = useState(false);
  return (
    <div data-testid="card" className="glass-panel">
      <button type="button" onClick={() => setOpen(true)}>
        Open
      </button>
      <Modal isOpen={open} onClose={() => setOpen(false)} title="Model gallery">
        {/* biome-ignore lint/a11y/noAutofocus: tests that an autoFocus field keeps the focus */}
        <input aria-label="Search" autoFocus={autoFocus} />
        <button type="button">Pick</button>
      </Modal>
    </div>
  );
}

const tab = (shiftKey = false) =>
  fireEvent.keyDown(document.activeElement as Element, {
    key: "Tab",
    shiftKey,
  });

describe("Modal", () => {
  it("renders into document.body, outside the card that opened it", () => {
    render(<Opener />);
    fireEvent.click(screen.getByRole("button", { name: "Open" }));

    const dialog = screen.getByRole("dialog", { name: "Model gallery" });
    expect(dialog.getAttribute("aria-modal")).toBe("true");
    expect(screen.getByTestId("card").contains(dialog)).toBe(false);
    expect(dialog.parentElement?.parentElement).toBe(document.body);
  });

  it("moves focus inside, keeps Tab inside, and returns focus to the opener on Escape", () => {
    render(<Opener />);
    const opener = screen.getByRole("button", { name: "Open" });
    opener.focus();
    fireEvent.click(opener);

    expect(document.activeElement).toBe(
      screen.getByRole("button", { name: "Close" }),
    );
    screen.getByRole("button", { name: "Pick" }).focus();
    tab();
    expect(document.activeElement).toBe(
      screen.getByRole("button", { name: "Close" }),
    );
    tab(true);
    expect(document.activeElement).toBe(
      screen.getByRole("button", { name: "Pick" }),
    );

    fireEvent.keyDown(document, { key: "Escape" });
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(document.activeElement).toBe(opener);
  });

  it("leaves the focus on an autoFocus field and still returns it to the opener", () => {
    render(<Opener autoFocus />);
    const opener = screen.getByRole("button", { name: "Open" });
    opener.focus();
    fireEvent.click(opener);

    expect(document.activeElement).toBe(screen.getByLabelText("Search"));
    fireEvent.click(screen.getByRole("button", { name: "Close" }));
    expect(document.activeElement).toBe(opener);
  });

  it("closes on a backdrop click but not on a click inside", () => {
    render(<Opener />);
    fireEvent.click(screen.getByRole("button", { name: "Open" }));

    fireEvent.click(screen.getByRole("button", { name: "Pick" }));
    expect(screen.getByRole("dialog")).toBeTruthy();
    fireEvent.click(screen.getByRole("dialog").parentElement as HTMLElement);
    expect(screen.queryByRole("dialog")).toBeNull();
  });
});

describe("ImageLightbox", () => {
  it("is a labelled dialog in document.body that Escape closes", () => {
    let closed = false;
    const { container } = render(
      <ImageLightbox
        src="blob:test/1"
        alt="Variation 1"
        onClose={() => {
          closed = true;
        }}
      />,
    );

    const dialog = screen.getByRole("dialog", { name: "Variation 1" });
    expect(container.contains(dialog)).toBe(false);
    expect(document.activeElement).toBe(
      screen.getByRole("button", { name: "Close" }),
    );
    fireEvent.keyDown(document, { key: "Escape" });
    expect(closed).toBe(true);
  });
});
