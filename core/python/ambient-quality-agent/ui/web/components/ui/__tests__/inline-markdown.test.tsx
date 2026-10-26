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

import { InlineMarkdown } from "../inline-markdown";

function renderLine(text: string) {
  return render(
    <p>
      <InlineMarkdown text={text} />
    </p>,
  ).container;
}

describe("InlineMarkdown", () => {
  it("renders a code span as code, without its backticks", () => {
    const line = renderLine("called `schedule_visit` with the address");

    expect(screen.getByText("schedule_visit").tagName).toBe("CODE");
    expect(line.textContent).toBe("called schedule_visit with the address");
  });

  it("renders bold in the medium weight, without its asterisks", () => {
    const line = renderLine("**never** call `refund_order` twice");

    const bold = screen.getByText("never");
    expect(bold.tagName).toBe("STRONG");
    // The type scale has two weights; the browser's own bold is a third.
    expect(bold).toHaveClass("font-medium");
    expect(screen.getByText("refund_order").tagName).toBe("CODE");
    expect(line.textContent).toBe("never call refund_order twice");
  });

  it("leaves a lone backtick as written", () => {
    const line = renderLine("the agent's `reply was cut");

    expect(line.querySelector("code")).toBeNull();
    expect(line.textContent).toBe("the agent's `reply was cut");
  });
});
