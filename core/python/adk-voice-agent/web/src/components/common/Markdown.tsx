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

import type { ReactNode } from "react";
import { keyedByText } from "../../lib/keys";

/**
 * A small Markdown renderer for thread answers: headings, paragraphs (soft line
 * breaks joined), lists, quotes, fenced code, tables, rules, and inline code,
 * emphasis, and links. Relative links render as their text.
 */

type Block =
  | { kind: "heading"; level: number; text: string }
  | { kind: "paragraph"; text: string }
  | { kind: "list"; ordered: boolean; items: string[] }
  | { kind: "quote"; text: string }
  | { kind: "code"; text: string }
  | { kind: "table"; rows: string[][] }
  | { kind: "rule" };

const HEADING = /^(#{1,6})\s+(.*)$/;
const LIST_ITEM = /^\s*(?:([-*+])|(\d+)[.)])\s+(?:\[[ xX]\]\s+)?(.*)$/;
const FENCE = /^\s*(```|~~~)/;
const RULE = /^\s*([-*_])(\s*\1){2,}\s*$/;
const TABLE_ROW = /^\s*\|.*\|\s*$/;
const TABLE_DIVIDER = /^\s*\|?[\s:|-]+\|?\s*$/;

const cells = (row: string) =>
  row
    .trim()
    .replace(/^\||\|$/g, "")
    .split("|")
    .map((c) => c.trim());

function parseBlocks(source: string): Block[] {
  const lines = source.replace(/\r\n?/g, "\n").split("\n");
  const blocks: Block[] = [];
  let i = 0;
  while (i < lines.length) {
    const line = lines[i];
    if (!line.trim()) {
      i += 1;
    } else if (FENCE.test(line)) {
      const body: string[] = [];
      for (i += 1; i < lines.length && !FENCE.test(lines[i]); i += 1)
        body.push(lines[i]);
      blocks.push({ kind: "code", text: body.join("\n") });
      i += 1;
    } else if (HEADING.test(line)) {
      const [, hashes = "", text = ""] = HEADING.exec(line) ?? [];
      blocks.push({ kind: "heading", level: hashes.length, text });
      i += 1;
    } else if (RULE.test(line)) {
      blocks.push({ kind: "rule" });
      i += 1;
    } else if (TABLE_ROW.test(line)) {
      const rows: string[][] = [];
      for (; i < lines.length && TABLE_ROW.test(lines[i]); i += 1) {
        if (!TABLE_DIVIDER.test(lines[i])) rows.push(cells(lines[i]));
      }
      blocks.push({ kind: "table", rows });
    } else if (line.trimStart().startsWith(">")) {
      const body: string[] = [];
      for (; i < lines.length && lines[i].trimStart().startsWith(">"); i += 1) {
        body.push(lines[i].trimStart().replace(/^>\s?/, ""));
      }
      blocks.push({ kind: "quote", text: body.join(" ") });
    } else if (LIST_ITEM.test(line)) {
      const ordered = Boolean(LIST_ITEM.exec(line)?.[2]);
      const items: string[] = [];
      for (; i < lines.length && lines[i].trim(); i += 1) {
        const item = LIST_ITEM.exec(lines[i]);
        if (item) items.push(item[3]);
        else items[items.length - 1] += ` ${lines[i].trim()}`;
      }
      blocks.push({ kind: "list", ordered, items });
    } else {
      const body: string[] = [];
      for (; i < lines.length && lines[i].trim(); i += 1) {
        const next = lines[i];
        if (
          body.length &&
          (HEADING.test(next) || LIST_ITEM.test(next) || FENCE.test(next))
        )
          break;
        body.push(next.trim());
      }
      blocks.push({ kind: "paragraph", text: body.join(" ") });
    }
  }
  return blocks;
}

const INLINE =
  /`([^`]+)`|\[([^\]]+)\]\(([^)\s]+)\)|\*\*([^*]+)\*\*|__([^_]+)__|\*([^*\s][^*]*)\*|(?<![\w])_([^_\s][^_]*)_(?![\w])|(https?:\/\/[^\s)<]+)/g;

function inline(text: string): ReactNode[] {
  const out: ReactNode[] = [];
  let last = 0;
  for (const m of text.matchAll(INLINE)) {
    const at = m.index ?? 0;
    if (at > last) out.push(text.slice(last, at));
    const key = out.length;
    const [, code, linkText, href, bold, bold2, em, em2, url] = m;
    if (code) out.push(<code key={key}>{code}</code>);
    else if (linkText) {
      out.push(
        /^https?:/.test(href) ? (
          <a key={key} href={href} target="_blank" rel="noreferrer">
            {inline(linkText)}
          </a>
        ) : (
          <span key={key}>{inline(linkText)}</span>
        ),
      );
    } else if (bold || bold2)
      out.push(<strong key={key}>{inline(bold || bold2)}</strong>);
    else if (em || em2) out.push(<em key={key}>{inline(em || em2)}</em>);
    else if (url) {
      out.push(
        <a key={key} href={url} target="_blank" rel="noreferrer">
          {url.replace(/^https?:\/\//, "")}
        </a>,
      );
    }
    last = at + m[0].length;
  }
  if (last < text.length) out.push(text.slice(last));
  return out;
}

function renderBlock(block: Block, key: number): ReactNode {
  switch (block.kind) {
    case "heading": {
      const Tag = `h${Math.min(block.level + 1, 6)}` as "h2";
      return <Tag key={key}>{inline(block.text)}</Tag>;
    }
    case "paragraph":
      return <p key={key}>{inline(block.text)}</p>;
    case "list": {
      const Tag = block.ordered ? "ol" : "ul";
      return (
        <Tag key={key}>
          {keyedByText(block.items).map(({ key: itemKey, item }) => (
            <li key={itemKey}>{inline(item)}</li>
          ))}
        </Tag>
      );
    }
    case "quote":
      return <blockquote key={key}>{inline(block.text)}</blockquote>;
    case "code":
      return (
        <pre key={key}>
          <code>{block.text}</code>
        </pre>
      );
    case "table": {
      const [head, ...body] = block.rows;
      return (
        <div key={key} className="md-table">
          <table>
            <thead>
              <tr>
                {keyedByText(head ?? []).map(({ key: cellKey, item: c }) => (
                  <th key={cellKey}>{inline(c)}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {keyedByText(body, (row) => row.join("|")).map(
                ({ key: rowKey, item: row }) => (
                  <tr key={rowKey}>
                    {keyedByText(row).map(({ key: cellKey, item: c }) => (
                      <td key={cellKey}>{inline(c)}</td>
                    ))}
                  </tr>
                ),
              )}
            </tbody>
          </table>
        </div>
      );
    }
    case "rule":
      return <hr key={key} />;
  }
}

export function Markdown({ source }: { source: string }) {
  return <div className="md">{parseBlocks(source).map(renderBlock)}</div>;
}
