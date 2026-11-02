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

import type { ChatMessage, MessageSegment } from "@/components/chat/chat-shell";

const ROLE_LABEL: Record<ChatMessage["role"], string> = {
  user: "User",
  assistant: "Assistant",
  system: "System",
};

function segmentToMarkdown(seg: MessageSegment): string {
  switch (seg.kind) {
    case "text":
      return seg.text;
    case "tool": {
      const args = seg.args ? ` ${JSON.stringify(seg.args)}` : "";
      return `> 🔧 \`${seg.name}\`${args}`;
    }
    case "file":
      return `_[file: ${seg.file.name ?? seg.file.mimeType ?? "attachment"}]_`;
    case "data":
      return ["```json", JSON.stringify(seg.data, null, 2), "```"].join("\n");
    case "clarify":
      return `> ❓ ${seg.question}`;
    case "confirmation":
      return `> ⚠️ ${seg.hint}`;
  }
}

export function conversationToMarkdown(
  messages: ChatMessage[],
  title = "Chat",
): string {
  const body = messages
    .map((m) => {
      const header = `## ${ROLE_LABEL[m.role]} · ${new Date(m.createdAt).toISOString()}`;
      const segs = m.segments
        .map(segmentToMarkdown)
        .filter(Boolean)
        .join("\n\n");
      return `${header}\n\n${segs}`;
    })
    .join("\n\n");
  return `# ${title}\n\n${body}\n`;
}

export function conversationToJson(messages: ChatMessage[]): string {
  return JSON.stringify(messages, null, 2);
}

function slugify(title: string): string {
  const base = title
    .trim()
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "");
  return base || "chat";
}

// Browser-only: serialize the conversation and trigger a file download.
export function downloadConversation(
  messages: ChatMessage[],
  title: string,
  format: "markdown" | "json",
): void {
  const isMd = format === "markdown";
  const content = isMd
    ? conversationToMarkdown(messages, title)
    : conversationToJson(messages);
  const blob = new Blob([content], {
    type: isMd ? "text/markdown" : "application/json",
  });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = `${slugify(title)}.${isMd ? "md" : "json"}`;
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(url);
}
