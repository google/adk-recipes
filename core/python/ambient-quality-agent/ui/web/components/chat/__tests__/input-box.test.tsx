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

import { useState } from "react";
import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { InputBox, type InputBoxProps } from "@/components/chat/input-box";

function Harness(props: Partial<InputBoxProps> & { initialValue?: string }) {
  const [value, setValue] = useState(props.initialValue ?? "");
  return (
    <InputBox
      value={value}
      onValueChange={setValue}
      onSend={props.onSend ?? (() => {})}
      onStop={props.onStop}
      disabled={props.disabled}
      busy={props.busy}
      attachments={[]}
      onAddFiles={() => {}}
      onRemoveAttachment={() => {}}
      attachmentError={null}
      queued={props.queued ?? []}
      onRemoveQueued={props.onRemoveQueued ?? (() => {})}
    />
  );
}

describe("InputBox — controlled value + submit", () => {
  it("reflects typed text and clears on submit", async () => {
    const user = userEvent.setup();
    const onSend = vi.fn();
    render(<Harness onSend={onSend} />);
    const textarea = screen.getByRole("textbox");
    await user.type(textarea, "hello{Enter}");
    expect(onSend).toHaveBeenCalledWith("hello");
    expect((textarea as HTMLTextAreaElement).value).toBe("");
  });

  it("submits (to be queued) even while busy", async () => {
    const user = userEvent.setup();
    const onSend = vi.fn();
    render(<Harness onSend={onSend} busy onStop={() => {}} />);
    const textarea = screen.getByRole("textbox");
    await user.type(textarea, "later{Enter}");
    expect(onSend).toHaveBeenCalledWith("later");
  });

  it("shows the Stop button (not Send) while busy", () => {
    render(<Harness busy onStop={() => {}} initialValue="x" />);
    expect(screen.getByLabelText("Stop generating")).toBeInTheDocument();
    expect(screen.queryByLabelText("Send message")).not.toBeInTheDocument();
  });

  it("disables attaching files while busy (text-only queue)", () => {
    render(<Harness busy onStop={() => {}} />);
    expect(screen.getByLabelText("Attach files")).toBeDisabled();
  });
});

describe("InputBox — queued messages", () => {
  it("renders each queued message", () => {
    render(<Harness queued={["first", "second"]} busy onStop={() => {}} />);
    expect(screen.getByText("first")).toBeInTheDocument();
    expect(screen.getByText("second")).toBeInTheDocument();
  });

  it("removes a queued message by index on click", async () => {
    const user = userEvent.setup();
    const onRemoveQueued = vi.fn();
    render(
      <Harness
        queued={["first", "second"]}
        onRemoveQueued={onRemoveQueued}
        busy
        onStop={() => {}}
      />,
    );
    await user.click(screen.getByLabelText("Remove queued message 1"));
    expect(onRemoveQueued).toHaveBeenCalledWith(0);
  });
});
