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
import { type Inline, inlines } from "@/lib/aqa-markdown";
import { mono } from "@/lib/typography";

/**
 * Renders a line of model-written text, such as an insight's label, with its
 * code spans as code and its bold as the medium weight. Any other markdown
 * shows as written. Sets no size or color, so it takes the role it sits in.
 *
 * Not the Markdown component: that one lays out blocks and loads a markdown
 * parser the dashboard pages otherwise do without.
 */
export function InlineMarkdown({ text }: { text: string }) {
  return <>{inlines(text).map(renderInline)}</>;
}

function renderInline(node: Inline, index: number): ReactNode {
  switch (node.t) {
    case "code":
      return (
        <code key={index} className={mono}>
          {node.v}
        </code>
      );
    case "strong":
      return (
        <strong key={index} className="font-medium">
          {node.v.map(renderInline)}
        </strong>
      );
    case "text":
      return node.v;
  }
}
