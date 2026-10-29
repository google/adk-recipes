/**
 * Copyright 2026 Google LLC
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     https://www.apache.org/licenses/LICENSE-2.0
 *
 * Unless required by applicable law or agreed to in writing, software
 * distributed under the License is distributed on an "AS IS" BASIS,
 * WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 * See the License for the specific language governing permissions and
 * limitations under the License.
 */

// biome-ignore-all lint/suspicious/noArrayIndexKey: the parsed text has no ids: a block or a span is its position

import { Fragment, type ReactNode } from "react";

/**
 * The small Markdown subset the Studio agent writes: paragraphs, "-"/"*"
 * and numbered lists, **bold**, *italic* / _italic_, `code` and https links.
 * Built as React elements (never HTML), so a reply cannot inject markup. A link
 * whose text is not its URL also shows where it goes, "text (example.com)":
 * a prompt-injected reply cannot hide a phishing site behind friendly text.
 */

const INLINE =
  /(\*\*[^*]+\*\*|`[^`]+`|\[[^\]]+\]\([^)\s]+\)|\*[^*\s][^*]*\*|_[^_\s][^_]*_)/g;
const BULLET = /^\s*[-*•]\s+/;
const NUMBERED = /^\s*\d+[.)]\s+/;

function httpsOnly(url: string): string {
  try {
    return new URL(url).protocol === "https:" ? url : "";
  } catch {
    return "";
  }
}

/** The host a link goes to, unless its text already shows the whole URL. */
function shownHost(text: string, href: string): string {
  const url = new URL(href);
  const bare = (u: string) =>
    u
      .trim()
      .replace(/^https:\/\//i, "")
      .replace(/\/$/, "")
      .toLowerCase();
  return bare(text) === bare(href) ? "" : url.hostname;
}

function link(key: number, text: string, href: string): ReactNode {
  const host = shownHost(text, href);
  return (
    <Fragment key={key}>
      <a
        href={href}
        target="_blank"
        rel="noreferrer"
        className="text-gm-accent underline"
      >
        {text}
      </a>
      {host && <span className="text-gm-text-secondary"> ({host})</span>}
    </Fragment>
  );
}

function inline(text: string): ReactNode[] {
  return text.split(INLINE).map((part, i) => {
    if (part.startsWith("**") && part.endsWith("**") && part.length > 4)
      return (
        <strong key={i} className="font-semibold">
          {part.slice(2, -2)}
        </strong>
      );
    if (part.startsWith("`") && part.endsWith("`") && part.length > 2)
      return (
        <code key={i} className="px-1 rounded bg-white/10 text-[0.85em]">
          {part.slice(1, -1)}
        </code>
      );
    const linked = /^\[([^\]]+)\]\(([^)\s]+)\)$/.exec(part);
    if (linked) {
      const href = httpsOnly(linked[2]);
      return href ? link(i, linked[1], href) : linked[1];
    }
    if (/^([*_]).+\1$/.test(part) && part.length > 2)
      return <em key={i}>{part.slice(1, -1)}</em>;
    return part;
  });
}

type Block = { kind: "p" | "ul" | "ol"; lines: string[] };

function blocks(text: string): Block[] {
  const out: Block[] = [];
  for (const line of text.replace(/\r\n/g, "\n").split("\n")) {
    const kind = BULLET.test(line)
      ? "ul"
      : NUMBERED.test(line)
        ? "ol"
        : line.trim()
          ? "p"
          : null;
    const last = out[out.length - 1];
    if (!kind) {
      if (last) out.push({ kind: "p", lines: [] });
    } else if (last && last.kind === kind) {
      last.lines.push(line.replace(kind === "ul" ? BULLET : NUMBERED, ""));
    } else {
      out.push({
        kind,
        lines: [
          line.replace(
            kind === "ul" ? BULLET : kind === "ol" ? NUMBERED : "",
            "",
          ),
        ],
      });
    }
  }
  return out.filter((b) => b.lines.length);
}

export default function Markdown({ text }: { text: string }) {
  return (
    <div className="flex flex-col gap-2">
      {blocks(text).map((b, i) => {
        if (b.kind === "p") {
          return (
            <p key={i}>
              {b.lines.flatMap((l, j) =>
                j ? [<br key={`br${j}`} />, ...inline(l)] : inline(l),
              )}
            </p>
          );
        }
        const items = b.lines.map((l, j) => <li key={j}>{inline(l)}</li>);
        return b.kind === "ul" ? (
          <ul key={i} className="list-disc pl-5 flex flex-col gap-1">
            {items}
          </ul>
        ) : (
          <ol key={i} className="list-decimal pl-5 flex flex-col gap-1">
            {items}
          </ol>
        );
      })}
    </div>
  );
}
