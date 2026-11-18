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

import { diffLines } from "diff";
import {
  Highlight,
  themes,
  type Language,
  type PrismTheme,
} from "prism-react-renderer";
import { textStyle } from "@/lib/typography";
import { cn } from "@/lib/utils";
import { useResolvedTheme } from "@/lib/use-appearance";

// Markdown fences carry a language *name* (```python), not an extension.
// Unknown names fall back to "markup", which renders without tokenizing.
const LANG_ALIAS: Record<string, Language> = {
  python: "python",
  py: "python",
  typescript: "typescript",
  ts: "typescript",
  tsx: "tsx",
  javascript: "javascript",
  js: "javascript",
  jsx: "jsx",
  shell: "bash",
  sh: "bash",
  bash: "bash",
  zsh: "bash",
  console: "bash",
  markdown: "markdown",
  md: "markdown",
  rust: "rust",
  rs: "rust",
  go: "go",
  golang: "go",
  java: "java",
  kotlin: "kotlin",
  kt: "kotlin",
  swift: "swift",
  ruby: "ruby",
  rb: "ruby",
  php: "php",
  c: "c",
  cpp: "cpp",
  "c++": "cpp",
  csharp: "csharp",
  cs: "csharp",
  "c#": "csharp",
  sql: "sql",
  yaml: "yaml",
  yml: "yaml",
  toml: "toml",
  ini: "ini",
  dockerfile: "docker",
  docker: "docker",
  hcl: "hcl",
  terraform: "hcl",
  tf: "hcl",
  json: "json",
  jsonc: "json",
  json5: "json",
  css: "css",
  scss: "scss",
  sass: "sass",
  less: "less",
  html: "markup",
  xml: "markup",
  svg: "markup",
  graphql: "graphql",
  gql: "graphql",
  diff: "diff",
  lua: "lua",
  perl: "perl",
  r: "r",
  scala: "scala",
  dart: "dart",
  elixir: "elixir",
  ex: "elixir",
  erlang: "erlang",
  haskell: "haskell",
  hs: "haskell",
  clojure: "clojure",
  groovy: "groovy",
  powershell: "powershell",
  ps1: "powershell",
  makefile: "makefile",
  make: "makefile",
  protobuf: "protobuf",
  proto: "protobuf",
};

export function prismLanguageFromInfo(
  info: string | null | undefined,
): Language {
  if (!info) return "markup";
  return LANG_ALIAS[info.toLowerCase()] ?? "markup";
}

export function CodeBlock({
  code,
  language,
  maxLines = 200,
  theme = themes.vsDark,
}: {
  code: string;
  language: Language;
  maxLines?: number;
  theme?: PrismTheme;
}) {
  const lines = code.split("\n");
  const truncated = lines.length > maxLines;
  const display = truncated
    ? lines.slice(0, maxLines).join("\n") +
      `\n… (${lines.length - maxLines} more lines truncated)`
    : code;
  return (
    <Highlight theme={theme} code={display} language={language}>
      {({ style, tokens, getLineProps, getTokenProps }) => (
        <pre
          style={style}
          className={cn(
            "max-w-full overflow-x-auto rounded-md border p-2",
            textStyle.code,
          )}
        >
          {tokens.map((line, i) => {
            const { key: lineKey, ...lineProps } = getLineProps({ line });
            return (
              // biome-ignore lint/suspicious/noArrayIndexKey: the lines of a highlighted block have no identity beyond their position.
              <div key={i} {...lineProps}>
                <span
                  aria-hidden
                  // Fade the inherited Prism text color rather than a fixed token,
                  // so the gutter stays legible on both light and dark themes.
                  className="mr-2 inline-block w-8 select-none text-right opacity-40"
                >
                  {i + 1}
                </span>
                {line.map((token, j) => {
                  const { key: tokKey, ...tokProps } = getTokenProps({ token });
                  // biome-ignore lint/suspicious/noArrayIndexKey: the tokens of a line have no identity beyond their position.
                  return <span key={j} {...tokProps} />;
                })}
              </div>
            );
          })}
        </pre>
      )}
    </Highlight>
  );
}

export function DiffBlock({
  oldString,
  newString,
}: {
  oldString: string;
  newString: string;
}) {
  const dark = useResolvedTheme() === "dark";
  const parts = diffLines(oldString, newString);
  return (
    <pre
      className={cn(
        "max-w-full overflow-x-auto rounded-md border p-2",
        textStyle.code,
        dark ? "bg-zinc-950 text-zinc-100" : "bg-zinc-50 text-zinc-900",
      )}
    >
      {/* Block children inside an overflow-x-auto <pre> size to its visible
          viewport; wrap lines in a fit-content container so line backgrounds
          span the full scroll width of the longest line. */}
      <div className="w-fit min-w-full">
        {parts.flatMap((part, pi) => {
          const lines = part.value.split("\n");
          // diffLines puts a trailing "" after a final newline; drop it so we
          // don't render a phantom blank line per hunk.
          if (lines.length > 0 && lines[lines.length - 1] === "") lines.pop();
          const prefix = part.added ? "+" : part.removed ? "-" : " ";
          // Adjust text contrast across themes while maintaining green and red diff highlights.
          const cls = part.added
            ? cn(
                "bg-emerald-500/10",
                dark ? "text-emerald-300" : "text-emerald-700",
              )
            : part.removed
              ? cn("bg-rose-500/10", dark ? "text-rose-300" : "text-rose-700")
              : dark
                ? "text-zinc-400"
                : "text-zinc-500";
          return lines.map((ln, li) => (
            // biome-ignore lint/suspicious/noArrayIndexKey: diff lines have no identity beyond their position in the diff.
            <div key={`${pi}-${li}`} className={cn("px-1", cls)}>
              <span aria-hidden className="mr-2 select-none opacity-60">
                {prefix}
              </span>
              {ln || " "}
            </div>
          ));
        })}
      </div>
    </pre>
  );
}
