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

import type { CaseConversation, ConversationToolCall } from "@/lib/aqua-api";
import { timelineOf } from "@/lib/conversation";
import { Markdown } from "@/components/chat/markdown";
import { mono, textStyle } from "@/lib/typography";
import { cn } from "@/lib/utils";

/** A tool's arguments or its reply, for a <pre>. Empty when there is nothing:
 * a call whose response never arrived, and a reply whose call was truncated
 * away, both render with one side blank rather than as "null". */
function prettyJson(value: unknown): string {
  if (value === undefined || value === null) return "";
  return typeof value === "string" ? value : JSON.stringify(value, null, 2);
}

function ToolCall({ call }: { call: ConversationToolCall }) {
  const args = prettyJson(call.args);
  const response = prettyJson(call.response);

  return (
    <div
      className={cn(
        textStyle.code,
        "flex flex-col gap-1.5 rounded-md border bg-card/60 p-2.5",
        call.isError && "border-destructive/40 bg-destructive/5",
      )}
    >
      <div className="flex items-center gap-2">
        <span className="font-medium">{call.toolName ?? "(unnamed tool)"}</span>
        {call.isError && (
          <span
            className={cn(
              textStyle.label,
              "rounded border border-destructive bg-destructive/10 px-1 py-0.2 text-destructive",
            )}
          >
            Error
          </span>
        )}
      </div>

      {args && (
        <pre
          className={cn(
            textStyle.code,
            "overflow-x-auto rounded bg-muted/40 p-2",
          )}
        >
          {args}
        </pre>
      )}
      {response && (
        <pre
          className={cn(
            textStyle.code,
            "overflow-x-auto rounded bg-muted/40 p-2",
            call.isError && "text-destructive",
          )}
        >
          {response}
        </pre>
      )}
    </div>
  );
}

function ToolGroup({ calls }: { calls: ConversationToolCall[] }) {
  return (
    <div className="flex flex-col gap-2 my-2">
      {calls.map((call) => (
        <ToolCall
          key={call.callId ?? `${call.turnIndex}-${call.toolName}`}
          call={call}
        />
      ))}
    </div>
  );
}

export function ConversationView({
  conversation,
}: {
  conversation: CaseConversation;
}) {
  const items = timelineOf(conversation);

  if (items.length === 0) {
    return (
      <p className={cn(textStyle.meta, "p-4")}>
        This trajectory&apos;s artifact has no turns or tool calls.
      </p>
    );
  }

  return (
    <div className="flex flex-col gap-3">
      {items.map((item, i) => {
        if (item.kind === "text") {
          const isUser = item.author === "user";
          return (
            <article
              // biome-ignore lint/suspicious/noArrayIndexKey: conversation items carry no ids; the list is rebuilt from the same turns in the same order.
              key={`text-${item.turnIndex}-${i}`}
              className={cn(
                textStyle.body,
                "flex flex-col gap-1 rounded-lg border p-3",
                isUser
                  ? "border-sky-500/30 bg-sky-500/5 self-end max-w-[85%]"
                  : "border-border bg-card self-start max-w-[90%]",
              )}
            >
              <header className={cn(textStyle.label, mono)}>
                {item.author ?? "unknown"}
              </header>
              <Markdown text={item.text} />
            </article>
          );
        }
        if (item.kind === "emptyResponse") {
          return (
            <article
              // biome-ignore lint/suspicious/noArrayIndexKey: conversation items carry no ids; the list is rebuilt from the same turns in the same order.
              key={`empty-${item.turnIndex}-${i}`}
              className="flex flex-col gap-1 rounded-lg border border-destructive/30 bg-destructive/5 p-3 self-start max-w-[90%]"
            >
              <header className={cn(textStyle.label, mono)}>
                {item.author ?? "unknown"}
              </header>
              <p className={cn(textStyle.meta, "text-destructive")}>
                (no response)
              </p>
            </article>
          );
        }
        return (
          // biome-ignore lint/suspicious/noArrayIndexKey: conversation items carry no ids; the list is rebuilt from the same turns in the same order.
          <ToolGroup key={`tools-${item.turnIndex}-${i}`} calls={item.calls} />
        );
      })}
    </div>
  );
}
