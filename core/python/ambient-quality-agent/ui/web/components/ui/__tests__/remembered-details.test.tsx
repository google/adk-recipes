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

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";
import { RememberedDetails } from "../remembered-details";

function renderFold() {
  return render(
    <RememberedDetails storageKey="t.fold" summary="History">
      <p>older sighting</p>
    </RememberedDetails>,
  );
}

/** Open or close the fold the way a click on its summary does.
 *
 * @param open Whether to open the fold (`true`) or close it (`false`).
 */
function toggle(open: boolean) {
  const details = screen.getByText("History").closest("details");
  if (!details) throw new Error("no <details> around the summary");
  details.open = open;
  fireEvent(details, new Event("toggle"));
}

beforeEach(() => window.localStorage.clear());
afterEach(() => vi.restoreAllMocks());

describe("RememberedDetails", () => {
  it("starts closed when nothing is remembered", () => {
    renderFold();

    expect(screen.getByText("older sighting")).not.toBeVisible();
  });

  it("stays open on the next page once opened", () => {
    const { unmount } = renderFold();
    toggle(true);
    unmount();

    renderFold();

    expect(screen.getByText("older sighting")).toBeVisible();
  });

  it("stays closed on the next page once closed again", () => {
    window.localStorage.setItem("t.fold", "1");
    const { unmount } = renderFold();
    toggle(false);
    unmount();

    renderFold();

    expect(screen.getByText("older sighting")).not.toBeVisible();
  });

  it("still opens and closes when localStorage throws", () => {
    vi.spyOn(window, "localStorage", "get").mockImplementation(() => {
      throw new Error("denied");
    });
    renderFold();

    toggle(true);

    expect(screen.getByText("older sighting")).toBeVisible();
  });
});
