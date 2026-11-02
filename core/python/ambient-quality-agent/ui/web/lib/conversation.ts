/**
 * Copyright 2026 Google LLC
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 *
 * Unless required by applicable law or agreed to in writing, software
 * distributed under the License is distributed on an "AS IS" BASIS,
 * WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 * See the License for the specific language governing permissions and
 * limitations under the License.
 */

import type {
  CaseConversation,
  ConversationPart,
  ConversationToolCall,
} from "./aqua-api";

export interface TextItem {
  kind: "text";
  turnIndex: number;
  author: string | null;
  text: string;
}

export interface ToolGroupItem {
  kind: "toolGroup";
  turnIndex: number;
  calls: ConversationToolCall[];
}

/** A turn that happened but produced no text and no tool call. This is the
 * shape of `empty_final_response`, one of AQuA's two built-in metrics: a tool
 * call earlier in the case raised, and the turn that should have answered the
 * user has nothing in it. Told apart from a function-call event's empty text
 * (genuine noise) by the one thing that distinguishes them: that event's turn
 * also has a tool call recorded against it; this one does not. */
export interface EmptyResponseItem {
  kind: "emptyResponse";
  turnIndex: number;
  author: string | null;
}

export type TimelineItem = TextItem | ToolGroupItem | EmptyResponseItem;

/** The id a call and its response are matched on, falling back to the tool
 * name: `id` is optional in the SDK's part, and a turn that called one tool
 * once is still unambiguous without it. */
function pairingKey(
  part: NonNullable<ConversationPart["function_call" | "function_response"]>,
  index: number,
): string {
  return part.id || part.name || `#${index}`;
}

/** Whether a tool's reply reports a failure.
 *
 * There is no `is_error` on the wire -- ADK puts whatever the tool returned in
 * `response`, and a tool reports its own failure. An object with an `error` key
 * is the convention the observed agents follow, and the one AQuA's own tools
 * use when they refuse. Anything else is a success, including a response that
 * happens to contain the word. */
function reportsError(response: unknown): boolean {
  return (
    typeof response === "object" &&
    response !== null &&
    "error" in (response as Record<string, unknown>)
  );
}

/**
 * One case's turns as an ordered timeline, grouping a turn's tool calls the
 * way horizon's `message-bubble.tsx::groupSegments` groups consecutive `tool`
 * segments.
 *
 * Walks the stored shape directly -- turns, then each turn's events, then each
 * event's parts, all already in the order they happened. The fork read two flat
 * tables instead and could only order a turn's text against its tool calls by
 * convention, which is why its version rendered every bubble of a turn before
 * that turn's tools regardless of when they were called. Here a tool call sits
 * where it was made.
 *
 * One ADK turn holds both sides of an exchange -- the user's prompt and the
 * agent's reply share a `turn_index` with different `author`s -- so consecutive
 * text merges into one bubble only while the author holds. The bubble breaks
 * the moment it changes, the same rule `groupSegments` uses to break on kind.
 */
export function timelineOf(convo: CaseConversation): TimelineItem[] {
  const items: TimelineItem[] = [];

  for (const [position, turn] of (convo.turns ?? []).entries()) {
    const turnIndex = turn.turn_index ?? position;
    // Pairing spans the whole turn, so a response that arrives after the group
    // it belongs to has already been emitted still lands on its own call --
    // the entries are mutated in place, wherever they are by then.
    const byKey = new Map<string, ConversationToolCall>();
    let pending: ConversationToolCall[] = [];
    const before = items.length;
    let emptyAuthor: string | null | undefined;

    /** Close the run of tool calls, so what follows renders after them. */
    const flush = () => {
      if (pending.length === 0) return;
      items.push({ kind: "toolGroup", turnIndex, calls: pending });
      pending = [];
    };

    for (const event of turn.events ?? []) {
      const author = event.author ?? null;
      for (const [index, part] of (event.content?.parts ?? []).entries()) {
        if (part.function_call) {
          const call: ConversationToolCall = {
            callId: part.function_call.id ?? null,
            turnIndex,
            toolName: part.function_call.name ?? null,
            args: part.function_call.args,
            response: undefined,
            isError: false,
          };
          byKey.set(pairingKey(part.function_call, index), call);
          pending.push(call);
          continue;
        }
        if (part.function_response) {
          const key = pairingKey(part.function_response, index);
          const response = part.function_response.response;
          const call = byKey.get(key);
          if (call) {
            call.response = response;
            call.isError = reportsError(response);
          } else {
            // A reply whose call is missing: a truncated copy can lose the
            // event that made it. Shown with no arguments rather than dropped.
            const orphan: ConversationToolCall = {
              callId: part.function_response.id ?? null,
              turnIndex,
              toolName: part.function_response.name ?? null,
              args: undefined,
              response,
              isError: reportsError(response),
            };
            byKey.set(key, orphan);
            pending.push(orphan);
          }
          continue;
        }
        if (part.text) {
          flush();
          const last = items.at(-1);
          if (
            last?.kind === "text" &&
            last.turnIndex === turnIndex &&
            last.author === author
          ) {
            last.text += `\n${part.text}`;
          } else {
            items.push({ kind: "text", turnIndex, author, text: part.text });
          }
        } else if (part.text === "" && author !== "user") {
          emptyAuthor = author;
        }
      }
    }
    flush();

    // Nothing at all came of this turn: no text, no tool call. That is the
    // signal, not silence -- see `EmptyResponseItem`.
    if (items.length === before && emptyAuthor !== undefined) {
      items.push({ kind: "emptyResponse", turnIndex, author: emptyAuthor });
    }
  }

  return items;
}
