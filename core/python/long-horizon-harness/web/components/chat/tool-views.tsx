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
import { FileText, Pencil, Terminal as TerminalIcon } from "lucide-react";
import { cn } from "@/lib/utils";
import { CopyButton } from "./copy-button";
import { useViewerOptional } from "./artifact-viewer/viewer-context";
import { normalizeWorkspacePath } from "./workspace-href";

const EXT_TO_LANG: Record<string, Language> = {
  py: "python",
  ts: "typescript",
  tsx: "tsx",
  js: "javascript",
  jsx: "jsx",
  json: "json",
  md: "markdown",
  sh: "bash",
  bash: "bash",
  zsh: "bash",
  yaml: "yaml",
  yml: "yaml",
  html: "markup",
  css: "css",
  scss: "scss",
  go: "go",
  rs: "rust",
  java: "java",
  rb: "ruby",
  sql: "sql",
  toml: "toml",
};

function detectLanguage(path: string | null | undefined): Language {
  if (!path) return "markup";
  const dot = path.lastIndexOf(".");
  if (dot < 0) return "markup";
  const ext = path.slice(dot + 1).toLowerCase();
  return EXT_TO_LANG[ext] ?? "markup";
}

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
  ...EXT_TO_LANG,
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
          className="max-w-full overflow-x-auto rounded-md border p-2 text-[11px] font-mono leading-snug"
        >
          {tokens.map((line, i) => {
            const { key: lineKey, ...lineProps } = getLineProps({ line });
            return (
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
  const parts = diffLines(oldString, newString);
  return (
    <pre className="max-w-full overflow-x-auto rounded-md border bg-zinc-950 p-2 text-[11px] font-mono leading-snug text-zinc-100">
      {parts.flatMap((part, pi) => {
        const lines = part.value.split("\n");
        // diffLines puts a trailing "" after a final newline; drop it so we
        // don't render a phantom blank line per hunk.
        if (lines.length > 0 && lines[lines.length - 1] === "") lines.pop();
        const prefix = part.added ? "+" : part.removed ? "-" : " ";
        const cls = part.added
          ? "bg-emerald-500/10 text-emerald-300"
          : part.removed
            ? "bg-rose-500/10 text-rose-300"
            : "text-zinc-400";
        return lines.map((ln, li) => (
          <div key={`${pi}-${li}`} className={cn("px-1", cls)}>
            <span aria-hidden className="mr-2 select-none opacity-60">
              {prefix}
            </span>
            {ln || " "}
          </div>
        ));
      })}
    </pre>
  );
}

function asString(v: unknown): string {
  if (v == null) return "";
  return typeof v === "string" ? v : String(v);
}

function basename(path: string | null | undefined): string {
  if (!path) return "";
  const i = Math.max(path.lastIndexOf("/"), path.lastIndexOf("\\"));
  return i >= 0 ? path.slice(i + 1) : path;
}

type EditItem = { oldText?: unknown; newText?: unknown };

function asEditItems(v: unknown): EditItem[] {
  return Array.isArray(v) ? (v as EditItem[]) : [];
}

export function patchPreview(args: Record<string, unknown> | null): string {
  if (!args) return "";
  const path = asString(args.path);
  if (!path) return "";
  const edits = asEditItems(args.edits);
  let removed = 0;
  let added = 0;
  for (const e of edits) {
    const old = asString(e.oldText);
    const next = asString(e.newText);
    removed += old ? old.split("\n").length : 0;
    added += next ? next.split("\n").length : 0;
  }
  const suffix = edits.length > 1 ? ` (${edits.length} edits)` : "";
  return `${basename(path)} · −${removed} +${added}${suffix}`;
}

export function PatchView({
  args,
  result,
}: {
  args: Record<string, unknown> | null;
  result: unknown;
}) {
  const path = asString(args?.path);
  const edits = asEditItems(args?.edits);
  const err =
    typeof result === "object" && result && "error" in result
      ? asString((result as { error?: unknown }).error)
      : null;
  return (
    <div className="flex w-full max-w-full flex-col gap-1">
      <div className="text-[10px] uppercase tracking-wide text-muted-foreground">
        edit ·{" "}
        <span className="font-mono normal-case">{path || "(no path)"}</span>
        {edits.length > 1 && (
          <span className="ml-1 rounded bg-sky-500/15 px-1 text-sky-300">
            {edits.length} edits
          </span>
        )}
      </div>
      {edits.map((e, i) => (
        <DiffBlock
          key={i}
          oldString={asString(e.oldText)}
          newString={asString(e.newText)}
        />
      ))}
      {err && (
        <div className="rounded border border-rose-500/30 bg-rose-500/10 px-2 py-1 text-[11px] text-rose-300">
          {err}
        </div>
      )}
    </div>
  );
}

export function writeFilePreview(args: Record<string, unknown> | null): string {
  if (!args) return "";
  const path = asString(args.path);
  if (!path) return "";
  const content = asString(args.content);
  const lines = content ? content.split("\n").length : 0;
  return `${basename(path)} · ${lines} lines`;
}

/** The written path, clickable when it resolves to a live workspace file. */
function WorkspaceFileLink({ path }: { path: string }) {
  const viewer = useViewerOptional();
  const wsPath = path ? normalizeWorkspacePath(path) : null;
  if (!wsPath || !viewer) {
    return <span className="font-mono normal-case">{path || "(no path)"}</span>;
  }
  return (
    <button
      type="button"
      onClick={() =>
        viewer.openArtifact({
          name: wsPath.split("/").pop() || wsPath,
          mimeType: "",
          path: wsPath,
          url: `/lha/workspace/download?path=${encodeURIComponent(wsPath)}&inline=1`,
        })
      }
      className="font-mono normal-case underline hover:no-underline"
      title={`Open ${wsPath} in the side panel`}
    >
      {path}
    </button>
  );
}

export function WriteFileView({
  args,
  result,
}: {
  args: Record<string, unknown> | null;
  result: unknown;
}) {
  const path = asString(args?.path);
  const content = asString(args?.content);
  const lang = detectLanguage(path);
  const err =
    typeof result === "object" && result && "error" in result
      ? asString((result as { error?: unknown }).error)
      : null;
  return (
    <div className="flex w-full max-w-full flex-col gap-1">
      <div className="text-[10px] uppercase tracking-wide text-muted-foreground">
        write ·{" "}
        {err ? (
          <span className="font-mono normal-case">{path || "(no path)"}</span>
        ) : (
          <WorkspaceFileLink path={path} />
        )}
      </div>
      <CodeBlock code={content} language={lang} />
      {err && (
        <div className="rounded border border-rose-500/30 bg-rose-500/10 px-2 py-1 text-[11px] text-rose-300">
          {err}
        </div>
      )}
    </div>
  );
}

export function readFilePreview(args: Record<string, unknown> | null): string {
  if (!args) return "";
  const path = asString(args.path);
  if (!path) return "";
  const offset = typeof args.offset === "number" ? args.offset : null;
  const limit = typeof args.limit === "number" ? args.limit : null;
  if (offset != null && limit != null) {
    return `${basename(path)} · L${offset}–L${offset + limit - 1}`;
  }
  return basename(path);
}

export function ReadFileView({
  args,
  result,
  hasResult,
}: {
  args: Record<string, unknown> | null;
  result: unknown;
  hasResult: boolean;
}) {
  const path = asString(args?.path);
  const lang = detectLanguage(path);
  const r =
    typeof result === "object" && result !== null
      ? (result as {
          content?: unknown;
          error?: unknown;
          filename?: unknown;
          mime_type?: unknown;
          size_bytes?: unknown;
          note?: unknown;
        })
      : null;
  const content = asString(r?.content);
  const err = asString(r?.error);
  // A media read (image/PDF/audio/video) has neither content nor error: the
  // bytes were staged as an artifact, not returned as text. Without this
  // branch, ReadFileView rendered a silent empty code block for every media
  // read (final-review Fix 9).
  const mimeType = asString(r?.mime_type);
  const isMediaResult = hasResult && !err && !content && !!mimeType;
  return (
    <div className="flex w-full max-w-full flex-col gap-1">
      <div className="text-[10px] uppercase tracking-wide text-muted-foreground">
        read ·{" "}
        <span className="font-mono normal-case">{path || "(no path)"}</span>
      </div>
      {hasResult ? (
        err ? (
          <div className="rounded border border-rose-500/30 bg-rose-500/10 px-2 py-1 text-[11px] text-rose-300">
            {err}
          </div>
        ) : isMediaResult ? (
          <div className="text-[11px] text-muted-foreground">
            Loaded as media ({mimeType}
            {typeof r?.size_bytes === "number" ? `, ${r.size_bytes}B` : ""})
            {asString(r?.note) ? ` — ${asString(r?.note)}` : ""}
          </div>
        ) : (
          <CodeBlock code={content} language={lang} />
        )
      ) : (
        <div className="text-[11px] text-muted-foreground">
          awaiting content…
        </div>
      )}
    </div>
  );
}

export function terminalPreview(args: Record<string, unknown> | null): string {
  if (!args) return "";
  const cmd = asString(args.command);
  if (!cmd) return "";
  const single = cmd.replace(/\s+/g, " ").trim();
  return single.length > 60 ? single.slice(0, 60) + "…" : single;
}

export function TerminalView({
  args,
  result,
  hasResult,
}: {
  args: Record<string, unknown> | null;
  result: unknown;
  hasResult: boolean;
}) {
  const cmd = asString(args?.command);
  const r =
    typeof result === "object" && result !== null
      ? (result as {
          stdout?: unknown;
          stderr?: unknown;
          exit_code?: unknown;
          error?: unknown;
        })
      : null;
  const stdout = asString(r?.stdout);
  const stderr = asString(r?.stderr);
  const exitCode = typeof r?.exit_code === "number" ? r.exit_code : null;
  const err = asString(r?.error);
  return (
    <div className="flex w-full max-w-full flex-col gap-1">
      <div className="flex items-center gap-1.5 text-[10px] uppercase tracking-wide text-muted-foreground">
        bash
        {exitCode !== null && (
          <span
            className={cn(
              "rounded px-1 font-mono normal-case",
              exitCode === 0
                ? "bg-emerald-500/15 text-emerald-300"
                : "bg-rose-500/15 text-rose-300",
            )}
          >
            exit {exitCode}
          </span>
        )}
      </div>
      <pre className="max-w-full overflow-x-auto rounded-md border bg-zinc-950 p-2 text-[11px] font-mono leading-snug text-zinc-100">
        <span className="text-emerald-300">$</span> {cmd || "(no command)"}
      </pre>
      {hasResult && stdout && (
        <div className="group/term flex w-full max-w-full flex-col gap-0.5">
          <span className="text-[10px] uppercase tracking-wide text-muted-foreground">
            stdout
          </span>
          <div className="relative">
            <pre className="max-w-full overflow-x-auto rounded-md border bg-zinc-950 p-2 text-[11px] font-mono leading-snug text-zinc-100">
              {truncate(stdout)}
            </pre>
            <CopyButton
              getText={() => stdout}
              label="Copy stdout"
              className="absolute right-1.5 top-1.5 opacity-60 transition-opacity group-hover/term:opacity-100 max-md:opacity-100"
            />
          </div>
        </div>
      )}
      {hasResult && stderr && (
        <div className="group/term flex w-full max-w-full flex-col gap-0.5">
          <span className="text-[10px] uppercase tracking-wide text-muted-foreground">
            stderr
          </span>
          <div className="relative">
            <pre className="max-w-full overflow-x-auto rounded-md border border-rose-500/30 bg-rose-950/40 p-2 text-[11px] font-mono leading-snug text-rose-200">
              {truncate(stderr)}
            </pre>
            <CopyButton
              getText={() => stderr}
              label="Copy stderr"
              className="absolute right-1.5 top-1.5 opacity-60 transition-opacity group-hover/term:opacity-100 max-md:opacity-100"
            />
          </div>
        </div>
      )}
      {hasResult && err && (
        <div className="rounded border border-rose-500/30 bg-rose-500/10 px-2 py-1 text-[11px] text-rose-300">
          {err}
        </div>
      )}
      {!hasResult && (
        <div className="text-[11px] text-muted-foreground">running…</div>
      )}
    </div>
  );
}

function truncate(s: string, max = 4000): string {
  if (s.length <= max) return s;
  return s.slice(0, max) + `\n… (${s.length - max} more chars truncated)`;
}

export const TOOL_ICONS: Record<string, typeof Pencil> = {
  edit: Pencil,
  write: Pencil,
  read: FileText,
  bash: TerminalIcon,
};
