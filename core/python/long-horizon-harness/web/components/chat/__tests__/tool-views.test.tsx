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

import { describe, expect, it } from "vitest";
import { render, screen, within } from "@testing-library/react";

import {
  CodeBlock,
  DiffBlock,
  PatchView,
  ReadFileView,
  TerminalView,
  WriteFileView,
  patchPreview,
  readFilePreview,
  terminalPreview,
  writeFilePreview,
} from "@/components/chat/tool-views";

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
    // Each diff line is a <div> whose first <span> holds the prefix.
    const lines = Array.from(container.querySelectorAll("pre > div"));
    const summarized = lines.map((d) => d.textContent ?? "");
    expect(summarized).toContain("-b");
    expect(summarized).toContain("+B");
    expect(summarized).toContain(" a");
    expect(summarized).toContain(" c");
  });
});

describe("preview helpers", () => {
  it("patchPreview names the file and counts line deltas", () => {
    expect(
      patchPreview({
        path: "/tmp/foo/bar.py",
        edits: [{ oldText: "a\nb\nc", newText: "a\nB" }],
      }),
    ).toBe("bar.py · −3 +2");
  });

  it("patchPreview sums deltas across a batch and notes the edit count", () => {
    expect(
      patchPreview({
        path: "/tmp/foo/bar.py",
        edits: [
          { oldText: "a\nb\nc", newText: "a\nB" },
          { oldText: "x", newText: "y\nz" },
        ],
      }),
    ).toBe("bar.py · −4 +4 (2 edits)");
  });

  it("writeFilePreview reports basename + total line count", () => {
    expect(
      writeFilePreview({ path: "a/b/c.ts", content: "x\ny\nz" }),
    ).toBe("c.ts · 3 lines");
  });

  it("readFilePreview includes the offset range when present", () => {
    expect(
      readFilePreview({ path: "a/b.md", offset: 10, limit: 50 }),
    ).toBe("b.md · L10–L59");
    expect(readFilePreview({ path: "a/b.md" })).toBe("b.md");
  });

  it("terminalPreview compacts whitespace and truncates long commands", () => {
    expect(terminalPreview({ command: "ls   -la\n/tmp" })).toBe("ls -la /tmp");
    const long = "a".repeat(80);
    expect(terminalPreview({ command: long })).toHaveLength(61);
  });
});

describe("PatchView", () => {
  it("renders the path header + a diff body and surfaces errors", () => {
    render(
      <PatchView
        args={{
          path: "/tmp/x.py",
          edits: [{ oldText: "old line", newText: "new line" }],
        }}
        result={{ success: false, error: "edits[0]: oldText not found in /tmp/x.py." }}
      />,
    );
    // Path appears in the header (and the mono span inside it) — accept either.
    expect(screen.getAllByText(/\/tmp\/x\.py/).length).toBeGreaterThan(0);
    expect(screen.getByText(/oldText not found/)).toBeInTheDocument();
    // Diff lines render with - / + prefixes.
    expect(screen.getByText("old line")).toBeInTheDocument();
    expect(screen.getByText("new line")).toBeInTheDocument();
  });

  it("badges a multi-edit batch call", () => {
    render(
      <PatchView
        args={{
          path: "a.py",
          edits: [
            { oldText: "x", newText: "y" },
            { oldText: "p", newText: "q" },
          ],
        }}
        result={null}
      />,
    );
    expect(screen.getByText("2 edits")).toBeInTheDocument();
  });

  it("renders one diff block per edit in the batch", () => {
    const { container } = render(
      <PatchView
        args={{
          path: "a.py",
          edits: [
            { oldText: "x", newText: "y" },
            { oldText: "p", newText: "q" },
          ],
        }}
        result={null}
      />,
    );
    expect(container.querySelectorAll("pre").length).toBe(2);
  });
});

describe("WriteFileView", () => {
  it("shows the path and renders content as a code block", () => {
    const { container } = render(
      <WriteFileView
        args={{ path: "src/a.py", content: "def f():\n    return 1" }}
        result={{ success: true, path: "/abs/src/a.py" }}
      />,
    );
    expect(screen.getByText(/src\/a\.py/)).toBeInTheDocument();
    expect(container.textContent).toContain("def f():");
    expect(container.textContent).toContain("return 1");
  });
});

describe("ReadFileView", () => {
  it("renders the returned content when result is available", () => {
    const { container } = render(
      <ReadFileView
        args={{ path: "x.json", offset: 1, limit: 100 }}
        result={{ success: true, content: '{"a":1}' }}
        hasResult
      />,
    );
    expect(container.textContent).toContain('{"a":1}');
  });

  it("shows a placeholder when the result hasn't arrived yet", () => {
    render(
      <ReadFileView
        args={{ path: "x.txt" }}
        result={null}
        hasResult={false}
      />,
    );
    expect(screen.getByText(/awaiting content/i)).toBeInTheDocument();
  });

  it("surfaces an error in red when read failed", () => {
    render(
      <ReadFileView
        args={{ path: "missing.txt" }}
        result={{ success: false, error: "File not found: missing.txt" }}
        hasResult
      />,
    );
    expect(screen.getByText(/File not found/)).toBeInTheDocument();
  });
});

describe("TerminalView", () => {
  it("renders command + stdout + stderr + exit badge", () => {
    const { container } = render(
      <TerminalView
        args={{ command: "ls /tmp" }}
        result={{
          stdout: "a.txt\nb.txt",
          stderr: "warn: foo",
          exit_code: 0,
        }}
        hasResult
      />,
    );
    expect(container.textContent).toContain("$ ls /tmp");
    expect(container.textContent).toContain("a.txt");
    expect(container.textContent).toContain("warn: foo");
    expect(screen.getByText(/exit 0/)).toBeInTheDocument();
  });

  it("colors the exit badge red when non-zero", () => {
    render(
      <TerminalView
        args={{ command: "false" }}
        result={{ exit_code: 1 }}
        hasResult
      />,
    );
    const badge = screen.getByText(/exit 1/);
    expect(badge.className).toMatch(/rose/);
  });

  it("shows a 'running…' placeholder when the result hasn't arrived", () => {
    render(
      <TerminalView
        args={{ command: "sleep 1" }}
        result={null}
        hasResult={false}
      />,
    );
    expect(screen.getByText(/running…/i)).toBeInTheDocument();
  });

  it("does not render an empty stdout/stderr section", () => {
    const { container } = render(
      <TerminalView
        args={{ command: "true" }}
        result={{ stdout: "", stderr: "", exit_code: 0 }}
        hasResult
      />,
    );
    const text = container.textContent ?? "";
    expect(text).not.toMatch(/stdout/i);
    expect(text).not.toMatch(/stderr/i);
  });
});

describe("ToolRow integration via MessageBubble dispatch", () => {
  // Smoke: a `patch` tool segment routes through PatchView when expanded.
  // We assert against the rendered DOM in the bubble test below; this is
  // just a sanity scope check that the views render without provider setup.
  it("PatchView mounts without throwing on null result", () => {
    const { container } = render(
      <PatchView
        args={{ path: "a.py", edits: [{ oldText: "x", newText: "y" }] }}
        result={null}
      />,
    );
    expect(within(container).getByText(/a\.py/)).toBeInTheDocument();
  });
});
