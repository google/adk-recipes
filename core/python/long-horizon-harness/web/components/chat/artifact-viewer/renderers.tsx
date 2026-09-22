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

import { useMemo } from "react";
import { themes } from "prism-react-renderer";
import { Download } from "lucide-react";
import { useResolvedTheme } from "@/lib/use-appearance";
import { Markdown } from "../markdown";
import { CodeBlock, prismLanguageFromInfo } from "../tool-views";
import type { ArtifactTab } from "./viewer-store";

export type RendererKind =
  | "markdown"
  | "code"
  | "image"
  | "audio"
  | "html"
  | "json"
  | "pdf"
  | "download";

export type ViewMode = "preview" | "raw";

const CODE_EXT =
  /\.(ts|tsx|js|jsx|mjs|cjs|py|go|rs|java|c|cc|cpp|h|hpp|cs|rb|php|sh|bash|zsh|sql|ya?ml|toml|ini|cfg|conf|css|scss|less|xml|kt|swift|scala|lua|r|pl|dart|proto|gradle|txt|csv|tsv|log|env|dockerfile|makefile|gitignore)$/i;
const IMG_EXT = /\.(png|jpe?g|gif|webp|svg|bmp|ico|avif)$/i;

// Map an artifact's mime + filename onto the renderer that should show it.
export function pickRenderer(mimeType: string, name: string): RendererKind {
  const mime = (mimeType || "").toLowerCase();
  const n = (name || "").toLowerCase();
  if (mime === "text/markdown" || n.endsWith(".md") || n.endsWith(".markdown"))
    return "markdown";
  if (mime.startsWith("image/") || IMG_EXT.test(n)) return "image";
  if (mime === "application/pdf" || n.endsWith(".pdf")) return "pdf";
  if (mime.startsWith("audio/")) return "audio";
  if (mime === "text/html" || n.endsWith(".html") || n.endsWith(".htm"))
    return "html";
  if (mime === "application/json" || n.endsWith(".json")) return "json";
  if (mime.startsWith("text/") || CODE_EXT.test(n)) return "code";
  return "download";
}

// Only markdown + html have a distinct rendered view worth toggling to source.
export function supportsPreview(kind: RendererKind): boolean {
  return kind === "markdown" || kind === "html";
}

// Kinds whose bytes are UTF-8 text, so they can be fetched into an editor.
export function isTextKind(kind: RendererKind): boolean {
  return (
    kind === "markdown" || kind === "html" || kind === "json" || kind === "code"
  );
}

export function encodeText(text: string): string {
  const bytes = new TextEncoder().encode(text);
  let bin = "";
  for (const b of bytes) bin += String.fromCharCode(b);
  return btoa(bin);
}

function bytesToUint8(b64: string): Uint8Array {
  const bin = atob(b64);
  const out = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
  return out;
}

/**
 * Whether the tab's bytes are valid UTF-8, i.e. whether decoding them is
 * lossless. `decodeText` replaces invalid sequences with U+FFFD, so a latin-1
 * `.csv` decodes "successfully" — and saving that decode back would destroy
 * the original bytes.
 */
export function isLosslessText(tab: ArtifactTab): boolean {
  if (tab.bytes == null) return false;
  try {
    new TextDecoder("utf-8", { fatal: true }).decode(
      bytesToUint8(tab.bytes) as BufferSource,
    );
    return true;
  } catch {
    return false;
  }
}

export function decodeText(tab: ArtifactTab): string | null {
  if (tab.bytes == null) return null;
  try {
    return new TextDecoder().decode(bytesToUint8(tab.bytes));
  } catch {
    return null;
  }
}

export function dataUrl(tab: ArtifactTab): string | null {
  if (tab.url) return tab.url;
  if (tab.bytes == null) return null;
  return `data:${tab.mimeType || "application/octet-stream"};base64,${tab.bytes}`;
}

function langFromName(name: string): string {
  const ext = name.split(".").pop() ?? "";
  return ext.toLowerCase();
}

function Unavailable({ text }: { text: string }) {
  return (
    <div className="px-3 py-6 text-center text-xs text-muted-foreground">
      {text}
    </div>
  );
}

function DownloadView({ tab }: { tab: ArtifactTab }) {
  const href = dataUrl(tab);
  return (
    <div className="flex flex-col items-center gap-3 px-3 py-10 text-center">
      <p className="text-sm text-muted-foreground">
        <span className="font-mono text-foreground">{tab.name}</span>{" "}
        can&rsquo;t be previewed.
      </p>
      {href && (
        <a
          href={href}
          download={tab.name}
          className="inline-flex items-center gap-1.5 rounded-md border border-border bg-card px-3 py-1.5 text-xs font-medium hover:bg-muted"
        >
          <Download className="h-3.5 w-3.5" />
          Download
        </a>
      )}
    </div>
  );
}

function SourceView({ tab }: { tab: ArtifactTab }) {
  const theme = useResolvedTheme();
  const prismTheme = theme === "dark" ? themes.vsDark : themes.github;
  const text = decodeText(tab);
  const language = useMemo(
    () => prismLanguageFromInfo(langFromName(tab.name)),
    [tab.name],
  );
  if (text == null) return <Unavailable text="Source not available inline." />;
  return (
    <div className="p-2">
      <CodeBlock code={text} language={language} theme={prismTheme} />
    </div>
  );
}

function AudioView({ tab }: { tab: ArtifactTab }) {
  const src = dataUrl(tab);
  if (!src) return <Unavailable text="Audio not available." />;
  return (
    <div className="flex items-center justify-center p-4">
      <audio controls src={src} className="w-full max-w-md" />
    </div>
  );
}

function PdfView({ tab }: { tab: ArtifactTab }) {
  // Needs a URL the browser can navigate to: the viewer is the platform's own
  // PDF plugin, and a base64 data: URL is blocked for it in Chrome. The
  // workspace route serves PDFs inline under `Content-Security-Policy:
  // sandbox`, which is exactly what this needs.
  if (!tab.url) return <DownloadView tab={tab} />;
  return (
    <iframe title={tab.name} src={tab.url} className="h-full w-full border-0" />
  );
}

function ImageView({ tab }: { tab: ArtifactTab }) {
  const src = dataUrl(tab);
  if (!src) return <Unavailable text="Image not available." />;
  return (
    <div className="flex items-center justify-center p-4">
      <img
        src={src}
        alt={tab.name}
        className="max-h-full max-w-full rounded object-contain"
      />
    </div>
  );
}

function HtmlView({ tab, mode }: { tab: ArtifactTab; mode: ViewMode }) {
  const text = decodeText(tab);
  if (mode === "raw") return <SourceView tab={tab} />;
  if (text == null) {
    if (tab.url)
      return (
        <iframe
          title={tab.name}
          src={tab.url}
          sandbox=""
          className="h-full w-full border-0"
        />
      );
    return <Unavailable text="HTML not available inline." />;
  }
  // sandbox="" strips scripts/forms/same-origin — safe render of agent HTML.
  return (
    <iframe
      title={tab.name}
      srcDoc={text}
      sandbox=""
      className="h-full w-full border-0"
    />
  );
}

function JsonView({ tab }: { tab: ArtifactTab }) {
  const theme = useResolvedTheme();
  const prismTheme = theme === "dark" ? themes.vsDark : themes.github;
  const raw = decodeText(tab);
  const pretty = useMemo(() => {
    if (raw == null) return null;
    try {
      return JSON.stringify(JSON.parse(raw), null, 2);
    } catch {
      return raw;
    }
  }, [raw]);
  if (pretty == null) return <Unavailable text="JSON not available inline." />;
  return (
    <div className="p-2">
      <CodeBlock code={pretty} language="json" theme={prismTheme} />
    </div>
  );
}

export function ArtifactBody({
  tab,
  kind,
  mode,
}: {
  tab: ArtifactTab;
  kind: RendererKind;
  mode: ViewMode;
}) {
  switch (kind) {
    case "markdown":
      if (mode === "raw") return <SourceView tab={tab} />;
      return (
        <div className="px-3 py-2">
          <Markdown text={decodeText(tab) ?? ""} />
        </div>
      );
    case "html":
      return <HtmlView tab={tab} mode={mode} />;
    case "image":
      return <ImageView tab={tab} />;
    case "pdf":
      return <PdfView tab={tab} />;
    case "audio":
      return <AudioView tab={tab} />;
    case "json":
      return <JsonView tab={tab} />;
    case "code":
      return <SourceView tab={tab} />;
    default:
      return <DownloadView tab={tab} />;
  }
}
