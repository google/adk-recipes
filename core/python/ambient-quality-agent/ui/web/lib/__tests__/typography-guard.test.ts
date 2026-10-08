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

// Every piece of text sets its type through a role in lib/typography.ts. This
// test reads the string literals of every source file, and the `style` objects
// of its JSX, and fails on a class or property that sets type by hand. A string
// that only looks like a class is exempted with a comment on the line above:
// `// typography-guard: <reason>`.

import { readdirSync, readFileSync } from "node:fs";
import path from "node:path";
import ts from "typescript";
import { describe, expect, it } from "vitest";

const WEB_ROOT = path.resolve(__dirname, "../..");
const TYPOGRAPHY = "lib/typography.ts";
const MARKDOWN = "components/chat/markdown.tsx";
const isPrimitive = (file: string) => file.startsWith("components/ui/");

function listSources(dir: string): string[] {
  return readdirSync(path.join(WEB_ROOT, dir), { withFileTypes: true }).flatMap(
    (entry) => {
      const rel = path.join(dir, entry.name);
      if (entry.isDirectory()) {
        return entry.name === "__tests__" || entry.name === "__fixtures__"
          ? []
          : listSources(rel);
      }
      return /\.tsx?$/.test(entry.name) &&
        !/\.test\.tsx?$/.test(entry.name) &&
        entry.name !== "routeTree.gen.ts"
        ? [rel]
        : [];
    },
  );
}

/** Why a class is not allowed in `file`, or null when it is. */
function describeProblem(token: string, file: string): string | null {
  const parts = token.replace(/^!/, "").split(":");
  const utility = parts[parts.length - 1].replace(/^!/, "");
  if (parts.slice(0, -1).some((variant) => variant.startsWith("prose-"))) {
    return "a prose-* modifier: style markdown in theme.typography";
  }
  if (/^text-(xs|sm|base|xl)$/.test(utility)) {
    return file === TYPOGRAPHY || isPrimitive(file) ? null : "a size class";
  }
  if (/^text-(lg|[2-9]xl|caption|label|body|heading-[sm])$/.test(utility)) {
    return "a size off the scale";
  }
  if (/^text-\[(\d|\.\d|calc|clamp)/.test(utility)) return "an arbitrary size";
  if (/^(leading|tracking)-/.test(utility)) return "line height or spacing";
  if (/^font-\[/.test(utility)) return "an arbitrary font";
  if (
    /^font-(thin|extralight|light|semibold|bold|extrabold|black)$/.test(utility)
  ) {
    return "a weight other than font-normal or font-medium";
  }
  if (/^(font-mono|font-sans|font-serif|opsz-.+)$/.test(utility)) {
    return file === TYPOGRAPHY ? null : "a family or optical size";
  }
  if (utility === "uppercase") return "uppercase";
  if (/^(lowercase|capitalize|normal-case)$/.test(utility)) {
    return file === TYPOGRAPHY ? null : "a case class";
  }
  if (utility === "italic") return "italic";
  if (
    /^(underline|no-underline|underline-offset-.+|decoration-.+)$/.test(utility)
  ) {
    return file === TYPOGRAPHY ? null : "an underline";
  }
  if (/^prose(-sm|-on-primary)?$/.test(utility)) {
    return file === MARKDOWN ? null : "prose outside the Markdown component";
  }
  if (utility === "tabular-nums")
    return "tabular-nums, which body already sets";
  return null;
}

const STYLE_KEYS = new Set([
  "fontSize",
  "fontWeight",
  "fontFamily",
  "fontStyle",
  "lineHeight",
  "letterSpacing",
  "textTransform",
]);

/** The role to suggest for a line's offending classes. */
function suggestRole(tokens: string[]): string {
  const hasUtility = (re: RegExp) =>
    // biome-ignore lint/style/noNonNullAssertion: split() returns at least one element, so pop() is never undefined.
    tokens.some((t) => re.test(t.split(":").pop()!));
  if (hasUtility(/^(underline|decoration-|underline-offset-)/))
    return "link.inline or link.standalone";
  if (hasUtility(/^font-mono$/))
    return "textStyle.code, or mono inside another role";
  if (hasUtility(/^(text-(lg|[2-9]xl)|text-xl)$/))
    return "textStyle.pageTitle or textStyle.sectionTitle";
  if (hasUtility(/^text-base$/)) return "textStyle.sectionTitle";
  if (hasUtility(/^uppercase$/) || hasUtility(/^text-\[(0\.6|1[01]px|10px)/))
    return "textStyle.label";
  if (hasUtility(/^text-(xs|\[)/))
    return hasUtility(/^font-(medium|semibold|bold)$/)
      ? "textStyle.label"
      : "textStyle.meta";
  if (hasUtility(/^text-sm$/))
    return hasUtility(/^font-(medium|semibold|bold)$/)
      ? "textStyle.itemTitle"
      : "textStyle.body or textStyle.description";
  return "a role from textStyle";
}

function listViolations(file: string): string[] {
  const text = readFileSync(path.join(WEB_ROOT, file), "utf8");
  const source = ts.createSourceFile(file, text, ts.ScriptTarget.Latest, true);
  const lines = text.split("\n");
  const isExempt = (node: ts.Node) => {
    const line = source.getLineAndCharacterOfPosition(node.getStart()).line;
    return line > 0 && lines[line - 1].includes("typography-guard:");
  };
  const found = new Map<number, string[]>();
  const addFinding = (node: ts.Node, what: string) => {
    const line = source.getLineAndCharacterOfPosition(node.getStart()).line + 1;
    found.set(line, [...(found.get(line) ?? []), what]);
  };
  const visit = (node: ts.Node) => {
    if (
      (ts.isStringLiteral(node) ||
        ts.isNoSubstitutionTemplateLiteral(node) ||
        ts.isTemplateHead(node) ||
        ts.isTemplateMiddle(node) ||
        ts.isTemplateTail(node)) &&
      !isExempt(node)
    ) {
      for (const token of node.text.split(/\s+/).filter(Boolean)) {
        if (describeProblem(token, file)) addFinding(node, token);
      }
    }
    if (
      ts.isJsxAttribute(node) &&
      node.name.getText(source) === "style" &&
      node.initializer &&
      ts.isJsxExpression(node.initializer) &&
      node.initializer.expression &&
      ts.isObjectLiteralExpression(node.initializer.expression) &&
      !isExempt(node)
    ) {
      for (const prop of node.initializer.expression.properties) {
        const name = prop.name?.getText(source).replace(/["']/g, "");
        if (name && STYLE_KEYS.has(name)) addFinding(prop, `style.${name}`);
      }
    }
    ts.forEachChild(node, visit);
  };
  visit(source);
  return [...found.entries()].map(
    ([line, tokens]) =>
      `${file}:${line}  ${tokens.join(" ")}\n    → use ${suggestRole(tokens)} from "@/lib/typography"`,
  );
}

describe("typography guard", () => {
  const files = ["components", "src", "lib"].flatMap(listSources);

  it("finds the sources it guards", () => {
    expect(files).toContain(TYPOGRAPHY);
    expect(files).toContain(MARKDOWN);
    expect(files.length).toBeGreaterThan(100);
  });

  it("finds no type set by hand", () => {
    const found = files.flatMap(listViolations);
    expect(found, `\n${found.join("\n")}\n`).toEqual([]);
  });

  it("flags what it should and passes what it should", () => {
    const isFlagged = (token: string, file = "components/x.tsx") =>
      describeProblem(token, file) !== null;
    for (const token of [
      "text-xs",
      "md:text-sm",
      "text-lg",
      "text-label",
      "text-[11px]",
      "text-[0.6875rem]",
      "tracking-tight",
      "leading-snug",
      "font-semibold",
      "hover:font-bold",
      "font-mono",
      "uppercase",
      "italic",
      "underline",
      "hover:underline",
      "prose-p:my-1",
      "prose",
      "tabular-nums",
    ]) {
      expect(isFlagged(token), token).toBe(true);
    }
    for (const token of [
      "font-medium",
      "font-normal",
      "text-muted-foreground",
      "text-[hsl(var(--primary))]",
      "text-left",
      "truncate",
      "not-italic",
      "line-clamp-2",
    ]) {
      expect(isFlagged(token), token).toBe(false);
    }
    expect(isFlagged("text-sm", "components/ui/button.tsx")).toBe(false);
    expect(isFlagged("text-lg", "components/ui/button.tsx")).toBe(true);
    expect(isFlagged("font-mono", TYPOGRAPHY)).toBe(false);
    expect(isFlagged("uppercase", TYPOGRAPHY)).toBe(true);
    expect(isFlagged("prose-sm", MARKDOWN)).toBe(false);
  });
});
