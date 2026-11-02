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

import { render, screen, fireEvent } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { InputBox } from "../input-box";

type Props = React.ComponentProps<typeof InputBox>;

function setup(over: Partial<Props> = {}) {
  const onClearSelection = vi.fn();
  render(
    <InputBox
      value=""
      onValueChange={() => {}}
      onSend={() => {}}
      attachments={[]}
      onAddFiles={() => {}}
      onRemoveAttachment={() => {}}
      attachmentError={null}
      queued={[]}
      onRemoveQueued={() => {}}
      selectionChip={{ label: "Cats are small …" }}
      onClearSelection={onClearSelection}
      {...over}
    />,
  );
  return { onClearSelection };
}

describe("selection chip", () => {
  it("shows the snippet label", () => {
    setup();
    expect(screen.getByText(/Cats are small …/)).toBeTruthy();
  });

  it("clears on the x button", () => {
    const { onClearSelection } = setup();
    fireEvent.click(screen.getByRole("button", { name: /clear selection/i }));
    expect(onClearSelection).toHaveBeenCalled();
  });

  it("is absent when there is no selection", () => {
    setup({ selectionChip: null });
    expect(
      screen.queryByRole("button", { name: /clear selection/i }),
    ).toBeNull();
  });
});
