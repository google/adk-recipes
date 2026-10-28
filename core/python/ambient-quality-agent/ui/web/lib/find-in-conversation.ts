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

function segmentText(seg: MessageSegment): string {
  switch (seg.kind) {
    case "text":
      return seg.text;
    case "tool":
      return seg.name;
    case "clarify":
      return seg.question;
    case "confirmation":
      return seg.hint;
    case "file":
      return seg.file.name ?? "";
    default:
      return "";
  }
}

function messageSearchText(message: ChatMessage): string {
  return message.segments.map(segmentText).join("\n").toLowerCase();
}

// Ordered ids of messages whose searchable text contains `query`. Empty/blank
// query matches nothing.
export function findMatches(messages: ChatMessage[], query: string): string[] {
  const q = query.trim().toLowerCase();
  if (!q) return [];
  return messages
    .filter((m) => messageSearchText(m).includes(q))
    .map((m) => m.id);
}
