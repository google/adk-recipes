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

import type React from "react";
import { memo, useMemo } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { themes } from "prism-react-renderer";
import { cn } from "@/lib/utils";
import { useResolvedTheme } from "@/lib/use-appearance";
import { CopyButton } from "./copy-button";
import { useImageLightbox } from "./image-lightbox";
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
  // A bare relative link has nowhere to resolve to: AQuA has no workspace,
  // and the /lha/workspace download it used to point at answers 501.
  return href.startsWith("/") || href.startsWith("#") || href.startsWith("?")
    ? href
    : undefined;
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
        // Sizes, weights, spacing and colors come from `theme.typography` in
        // tailwind.config.ts. `break-words` wraps a long line of inline code at
        // the bubble's edge: models often paste a whole shell command as inline
        // code, not as a fenced block.
        "prose prose-sm max-w-none break-words",
        // The user's own bubble sits on a primary fill.
        inverted && "prose-on-primary",
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
