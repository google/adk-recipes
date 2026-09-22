"use client";
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

import {
  Bold,
  Code,
  Heading1,
  Heading2,
  Heading3,
  Italic,
  Link as LinkIcon,
  List,
  ListOrdered,
  ListTodo,
  Quote,
  SquareCode,
  Strikethrough,
  Table as TableIcon,
} from "lucide-react";
import { cn } from "@/lib/utils";
import type { MarkdownCommand } from "@/lib/markdown-commands";

interface Item {
  cmd: MarkdownCommand;
  label: string;
  Icon: typeof Bold;
}

// Grouped the way a document toolbar reads: emphasis, structure, blocks.
const GROUPS: Item[][] = [
  [
    { cmd: "bold", label: "Bold", Icon: Bold },
    { cmd: "italic", label: "Italic", Icon: Italic },
    { cmd: "strikethrough", label: "Strikethrough", Icon: Strikethrough },
    { cmd: "code", label: "Inline code", Icon: Code },
  ],
  [
    { cmd: "h1", label: "Heading 1", Icon: Heading1 },
    { cmd: "h2", label: "Heading 2", Icon: Heading2 },
    { cmd: "h3", label: "Heading 3", Icon: Heading3 },
  ],
  [
    { cmd: "bullet", label: "Bullet list", Icon: List },
    { cmd: "ordered", label: "Numbered list", Icon: ListOrdered },
    { cmd: "task", label: "Task list", Icon: ListTodo },
    { cmd: "quote", label: "Quote", Icon: Quote },
  ],
  [
    { cmd: "link", label: "Link", Icon: LinkIcon },
    { cmd: "codeblock", label: "Code block", Icon: SquareCode },
    { cmd: "table", label: "Table", Icon: TableIcon },
  ],
];

export function MarkdownToolbar({
  onCommand,
  disabled,
}: {
  onCommand: (cmd: MarkdownCommand) => void;
  disabled?: boolean;
}) {
  return (
    <div
      role="toolbar"
      aria-label="Formatting"
      className="flex shrink-0 flex-wrap items-center gap-0.5 border-b border-border/60 px-2 py-1"
    >
      {GROUPS.map((group, gi) => (
        <div key={gi} className="flex items-center gap-0.5">
          {gi > 0 && <span className="mx-1 h-4 w-px bg-border/60" />}
          {group.map(({ cmd, label, Icon }) => (
            <button
              key={cmd}
              type="button"
              aria-label={label}
              title={label}
              disabled={disabled}
              // The editor keeps its selection: without this the textarea
              // blurs on mousedown and the command has nothing to act on.
              onMouseDown={(e) => e.preventDefault()}
              onClick={() => onCommand(cmd)}
              className={cn(
                "inline-flex h-6 w-6 items-center justify-center rounded text-muted-foreground transition-colors",
                "hover:bg-muted hover:text-foreground",
                "disabled:cursor-not-allowed disabled:opacity-40 disabled:hover:bg-transparent",
              )}
            >
              <Icon className="h-3.5 w-3.5" />
            </button>
          ))}
        </div>
      ))}
    </div>
  );
}
