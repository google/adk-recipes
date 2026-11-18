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

// biome-ignore-all lint/style/noNonNullAssertion: a missing value fails the test either way; the assertion only narrows the type.

import { beforeEach, describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";

import { CodeBlock, DiffBlock } from "@/components/chat/tool-views";

describe("CodeBlock", () => {
  it("renders the source text with one rendered line per source line", () => {
    const { container } = render(
      <CodeBlock code={"print('hi')\nprint('there')"} language="python" />,
    );
    const pre = container.querySelector("pre");
    expect(pre).not.toBeNull();
    // One line wrapper per source line; ignore depth since prism may inject
    // its own tokens spans inside.
    expect(pre!.querySelectorAll("div").length).toBeGreaterThanOrEqual(2);
    expect(pre!.textContent).toContain("print('hi')");
    expect(pre!.textContent).toContain("print('there')");
  });

  it("truncates beyond maxLines and notes how many were dropped", () => {
    const lines = Array.from({ length: 250 }, (_, i) => `line ${i}`).join("\n");
    render(<CodeBlock code={lines} language="markup" maxLines={50} />);
    expect(screen.getByText(/200 more lines truncated/)).toBeInTheDocument();
  });
});

describe("DiffBlock", () => {
  it("renders + lines for additions and - lines for removals", () => {
    const { container } = render(
      <DiffBlock oldString={"a\nb\nc"} newString={"a\nB\nc"} />,
    );
    // Diff lines sit inside a fit-content wrapper under <pre>, with each line's
    // first <span> holding the +/-/space prefix.
    const lines = Array.from(container.querySelectorAll("pre > div > div"));
    const summarized = lines.map((d) => d.textContent ?? "");
    expect(summarized).toContain("-b");
    expect(summarized).toContain("+B");
    expect(summarized).toContain(" a");
    expect(summarized).toContain(" c");
  });

  it("wraps diff lines in a fit-content container so line highlights span the full scroll width", () => {
    const longLine = `instruction = "${"x".repeat(200)}"`;
    const { container } = render(
      <DiffBlock oldString={"short = 1"} newString={longLine} />,
    );
    const wrapper = container.querySelector("pre > div")!;
    expect(wrapper.className).toContain("w-fit");
    expect(wrapper.className).toContain("min-w-full");
    const lines = Array.from(wrapper.children);
    expect(lines).toHaveLength(2);
    expect(lines[0].className).toContain("bg-rose-500/10");
    expect(lines[1].className).toContain("bg-emerald-500/10");
  });

  // DiffBlock is rendered in both light and dark contexts across chat and insight panels.
  describe("theming", () => {
    // Reset theme before each render to avoid state updates outside act().
    beforeEach(() => document.documentElement.classList.remove("dark"));

    const addedLine = (container: HTMLElement) =>
      Array.from(container.querySelectorAll("pre > div > div")).find((d) =>
        (d.textContent ?? "").startsWith("+"),
      );

    it("uses a dark ground and light token colors under the dark theme", () => {
      document.documentElement.classList.add("dark");
      const { container } = render(
        <DiffBlock oldString={"a\nb"} newString={"a\nB"} />,
      );
      expect(container.querySelector("pre")!.className).toContain(
        "bg-zinc-950",
      );
      expect(addedLine(container)!.className).toContain("text-emerald-300");
    });

    it("uses a light ground and darker token colors under the light theme", () => {
      const { container } = render(
        <DiffBlock oldString={"a\nb"} newString={"a\nB"} />,
      );
      const pre = container.querySelector("pre")!;
      expect(pre.className).toContain("bg-zinc-50");
      expect(pre.className).not.toContain("bg-zinc-950");
      expect(addedLine(container)!.className).toContain("text-emerald-700");
    });
  });
});
