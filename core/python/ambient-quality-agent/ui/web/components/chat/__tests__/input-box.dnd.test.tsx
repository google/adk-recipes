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
import { render, screen, fireEvent } from "@testing-library/react";
import { InputBox } from "@/components/chat/input-box";

function renderBox(onAddFiles = vi.fn()) {
  render(
    <InputBox
      value=""
      onValueChange={() => {}}
      onSend={() => {}}
      attachments={[]}
      onAddFiles={onAddFiles}
      onRemoveAttachment={() => {}}
      attachmentError={null}
      queued={[]}
      onRemoveQueued={() => {}}
    />,
  );
  return onAddFiles;
}

describe("InputBox drag-and-drop", () => {
  it("routes dropped files to onAddFiles", () => {
    const onAddFiles = renderBox();
    const box = screen.getByRole("textbox");
    const file = new File(["hi"], "a.png", { type: "image/png" });
    fireEvent.drop(box, { dataTransfer: { files: [file], types: ["Files"] } });
    expect(onAddFiles).toHaveBeenCalledTimes(1);
    expect(onAddFiles.mock.calls[0][0][0].name).toBe("a.png");
  });

  it("shows a dropzone overlay while dragging files over", () => {
    renderBox();
    const box = screen.getByRole("textbox");
    fireEvent.dragOver(box, { dataTransfer: { types: ["Files"] } });
    expect(screen.getByText(/drop to attach/i)).toBeInTheDocument();
  });
});
