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

import React, { memo, useMemo } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { themes } from "prism-react-renderer";
import { cn } from "@/lib/utils";
import { useResolvedTheme } from "@/lib/use-appearance";
import { CopyButton } from "./copy-button";
import { useImageLightbox } from "./image-lightbox";
import { useViewerOptional } from "./artifact-viewer/viewer-context";
import { useBusy } from "./busy-context";
import { useWorkspaceFiles } from "@/lib/workspace-files";
import {
  workspacePathFromHref,
  workspacePathFromMention,
} from "./workspace-href";
import { CodeBlock, prismLanguageFromInfo } from "./tool-views";

// Module-level so React doesn't see a new array reference every render and
// invalidate ReactMarkdown's internal memoization.
const REMARK_PLUGINS = [remarkGfm];

// Route bare relative hrefs (e.g. the model writing `[plot.html](plot.html)`,
// which would otherwise 404 against the web origin) to the identity-scoped
// workspace download endpoint. Pass through http(s)/mailto/tel and app/anchor
// links; drop any other scheme (javascript:, data:, …) so agent-authored
// markdown can't smuggle an executable href.
function resolveHref(href?: string): string | undefined {
  if (!href) return href;
  const scheme = /^([a-z][a-z0-9+.-]*):/i.exec(href)?.[1]?.toLowerCase();
  if (scheme) {
    return ["http", "https", "mailto", "tel"].includes(scheme)
      ? href
      : undefined;
  }
  if (href.startsWith("/") || href.startsWith("#") || href.startsWith("?"))
    return href;
  return `/lha/workspace/download?path=${encodeURIComponent(href)}&inline=1`;
}

// Module-scope component map so each Markdown render doesn't recreate refs
// for ReactMarkdown's internal memoization.
const ANCHOR_COMPONENT = ({
  href,
  children,
}: {
  href?: string;
  children?: React.ReactNode;
}) => {
  const viewer = useViewerOptional();
  // A bare relative link is the model pointing at a workspace file it just
  // wrote. Open those in the side panel rather than punting to a new tab —
  // same destination, but it stays in the conversation.
  const wsPath = workspacePathFromHref(href);
  if (wsPath && viewer) {
    return (
      <OpenInPanelButton
        path={wsPath}
        className="underline [overflow-wrap:anywhere] hover:no-underline"
      >
        {children}
      </OpenInPanelButton>
    );
  }
  return (
    // overflow-wrap:anywhere lets long bare URLs break mid-string instead of
    // forcing the bubble wider than a phone viewport.
    <a
      href={resolveHref(href)}
      target="_blank"
      rel="noreferrer noopener"
      className="[overflow-wrap:anywhere]"
    >
      {children}
    </a>
  );
};

// Markdown images open full-screen in the lightbox instead of a new tab.
function MarkdownImage({ src, alt }: { src?: string; alt?: string }) {
  const openImage = useImageLightbox();
  if (!src) return null;
  const label = alt ?? "";
  return (
    <button
      type="button"
      onClick={() => openImage(src, label)}
      aria-label={`View image: ${label || "image"}`}
      className="block cursor-zoom-in rounded focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
    >
      <img src={src} alt={label} className="h-auto max-w-full rounded" />
    </button>
  );
}

/** Shared by the anchor and inline-code handlers. */
function OpenInPanelButton({
  path,
  className,
  children,
}: {
  path: string;
  className?: string;
  children?: React.ReactNode;
}) {
  const viewer = useViewerOptional();
  return (
    <button
      type="button"
      onClick={() =>
        viewer?.openArtifact({
          name: path.split("/").pop() || path,
          // pickRenderer falls back to the extension, which is all a
          // filename mention tells us.
          mimeType: "",
          path,
          url: `/lha/workspace/download?path=${encodeURIComponent(path)}&inline=1`,
        })
      }
      className={className}
      title={`Open ${path} in the side panel`}
    >
      {children}
    </button>
  );
}

// Models habitually write a filename as `cats.md` rather than a markdown
// link, so inline code is where most workspace-file mentions actually land.
// Only names that match a real file are linkified — a link opening an empty
// panel is worse than plain text.
const INLINE_CODE_COMPONENT = ({
  className,
  children,
  ...props
}: React.HTMLAttributes<HTMLElement>) => {
  const busy = useBusy();
  const files = useWorkspaceFiles(busy);
  const viewer = useViewerOptional();
  const plain = (
    <code className={className} {...props}>
      {children}
    </code>
  );
  // A fenced block arrives with `language-xxx`; those render through `pre`.
  if (className?.includes("language-")) return plain;
  if (!viewer || !files) return plain;
  const text = typeof children === "string" ? children : null;
  if (!text) return plain;
  const path = workspacePathFromMention(text);
  if (!path || !files.has(path)) return plain;
  return (
    <OpenInPanelButton
      path={path}
      className="underline decoration-dotted underline-offset-2 hover:decoration-solid"
    >
      {plain}
    </OpenInPanelButton>
  );
};

// Wide tables would otherwise burst the bubble and push the layout past a
// phone viewport. Wrap in an overflow container so they scroll horizontally.
const TABLE_COMPONENT = ({
  children,
  ...props
}: React.HTMLAttributes<HTMLTableElement>) => (
  <div className="max-w-full overflow-x-auto">
    <table {...props}>{children}</table>
  </div>
);

// react-markdown renders a fenced block as <pre><code class="language-xxx">.
// Pull the language name off that child so we can syntax-highlight it.
function fenceLanguage(children: unknown): string | null {
  const child = Array.isArray(children) ? children[0] : children;
  const className = (child as { props?: { className?: unknown } } | null)?.props
    ?.className;
  if (typeof className !== "string") return null;
  const match = className.match(/language-([\w-]+)/);
  return match ? match[1] : null;
}

export const Markdown = memo(function Markdown({
  text,
  inverted,
}: {
  text: string;
  inverted?: boolean;
}) {
  const theme = useResolvedTheme();
  const prismTheme = theme === "dark" ? themes.vsDark : themes.github;
  const components = useMemo(
    () => ({
      a: ANCHOR_COMPONENT,
      code: INLINE_CODE_COMPONENT,
      img: MarkdownImage,
      table: TABLE_COMPONENT,
      pre: ({ children }: React.HTMLAttributes<HTMLPreElement>) => {
        const code = extractCodeText(children).replace(/\n$/, "");
        const language = prismLanguageFromInfo(fenceLanguage(children));
        return (
          <div className="group/pre relative my-2">
            <CodeBlock code={code} language={language} theme={prismTheme} />
            {code && (
              <CopyButton
                getText={() => code}
                className="pointer-events-none absolute right-1.5 top-1.5 opacity-0 transition-opacity group-hover/pre:pointer-events-auto group-hover/pre:opacity-100 focus-within:pointer-events-auto focus-within:opacity-100 max-md:pointer-events-auto max-md:opacity-100"
              />
            )}
          </div>
        );
      },
    }),
    [prismTheme],
  );
  return (
    <div
      className={cn(
        // Prose colors are themed onto the app tokens in tailwind.config.ts, which
        // already flip under `.dark` — so no `prose-invert` here. `msg-prose`
        // scopes the user-selectable font-size.
        "msg-prose prose prose-sm max-w-none break-words",
        // Tighter paragraph rhythm than prose default, looser than 0.
        "[&>*:first-child]:mt-0 [&>*:last-child]:mb-0 prose-p:my-1.5 prose-p:leading-relaxed",
        // Fenced code blocks render through the `pre` handler (CodeBlock), which
        // owns its own padding/border/background — so no `prose-pre:*` here.
        // Inline code: real background contrast + monospace + tighter padding.
        // The default prose injects `\`` characters around inline code; hide them.
        // `break-words` lets long single-line commands wrap at the bubble edge
        // instead of overflowing — models often paste whole shell lines as
        // inline backticks rather than fenced blocks.
        "prose-code:before:hidden prose-code:after:hidden prose-code:rounded prose-code:px-1 prose-code:py-0.5 prose-code:font-mono prose-code:text-[0.9em] prose-code:font-normal prose-code:break-words [&_code]:[overflow-wrap:anywhere]",
        // Headings + lists tighten up.
        "prose-headings:mb-2 prose-headings:mt-3 prose-ul:my-1.5 prose-ol:my-1.5 prose-li:my-0.5",
        // Tables: tighter cells + a subtle header shade. The overflow-x-auto wrapper
        // lives on the `table` markdown handler so wide tables scroll on mobile.
        "prose-table:my-2 prose-th:px-2 prose-th:py-1 prose-th:bg-muted/40 prose-td:px-2 prose-td:py-1",
        // Surface treatment differs in inverted (purple user bubble) vs default.
        // Inverted sits on a solid bg-primary fill, so force prose colors to the
        // bubble's foreground (white) — the token theme would otherwise paint dark text.
        inverted
          ? "[--tw-prose-body:hsl(var(--primary-foreground))] [--tw-prose-headings:hsl(var(--primary-foreground))] [--tw-prose-bold:hsl(var(--primary-foreground))] [--tw-prose-links:hsl(var(--primary-foreground))] [--tw-prose-code:hsl(var(--primary-foreground))] [--tw-prose-bullets:hsl(var(--primary-foreground))] [--tw-prose-counters:hsl(var(--primary-foreground))] prose-a:underline prose-code:bg-white/20"
          : "prose-code:bg-foreground/10",
      )}
    >
      <ReactMarkdown remarkPlugins={REMARK_PLUGINS} components={components}>
        {text}
      </ReactMarkdown>
    </div>
  );
});

function extractCodeText(node: unknown): string {
  if (typeof node === "string") return node;
  if (typeof node === "number") return String(node);
  if (Array.isArray(node)) return node.map(extractCodeText).join("");
  if (node && typeof node === "object" && "props" in node) {
    const props = (node as { props?: { children?: unknown } }).props;
    return extractCodeText(props?.children);
  }
  return "";
}
