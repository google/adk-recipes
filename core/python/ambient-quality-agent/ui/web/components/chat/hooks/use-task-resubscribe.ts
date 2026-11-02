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

import { useEffect, useRef } from "react";
import type { HorizonClient } from "@/lib/a2a-client";
import { appendPartsToSegments } from "@/lib/chat-segments";
import { isResubscribeInactiveError } from "@/lib/is-resubscribe-inactive-error";
import { isTransportError } from "@/lib/is-transport-error";
import { extractParts, isFinal } from "@/lib/horizon-events";
import type { ChatMessage, MessageSegment } from "../chat-shell";

export interface UseTaskResubscribeArgs {
  activeTaskId: string | null;
  client: HorizonClient | null;
  // True while a live send/confirm stream owns the turn. Resubscribe must stay
  // dormant then — the live stream already renders the active task, and a
  // parallel resubscribe would render a duplicate bubble and fight the merge.
  // Resubscribe is for reconnect only (cold load / background task), not the
  // turn the user just sent in this session.
  suspended: boolean;
  setMessages: React.Dispatch<React.SetStateAction<ChatMessage[]>>;
  // Invalidator from useLhaTasks — called on terminal status so the task list
  // refetches, the active flag clears, and useTaskHistory pulls the now-complete
  // task into history.
  onTerminal: () => Promise<unknown> | undefined;
  // Fired when resubscribe fails with a transport-layer error (backend gone,
  // offline). Caller swaps the inline error trailer for a top-level banner.
  onSessionLost?: () => void;
  // Bumped on every stream frame so useTaskHistory knows the task moved. A ref
  // rather than a callback: a new identity each frame would restart this effect
  // and re-tap the SSE.
  progressRef?: { current: number };
}

// Live-bubble id is derived from the task id so we can find / remove it
// without holding a ref across renders.
function _liveBubbleId(taskId: string): string {
  return `live:${taskId}`;
}

interface GetTaskFallbackArgs {
  client: HorizonClient;
  taskId: string;
  bubbleId: string;
  signal: AbortSignal;
  setMessages: React.Dispatch<React.SetStateAction<ChatMessage[]>>;
}

// resubscribe could not stream this turn (task not active on this instance /
// already terminal). Pull the finished task via tasks/get and render its result
// into the live bubble so the user still sees the answer.
async function _renderViaGetTask({
  client,
  taskId,
  bubbleId,
  signal,
  setMessages,
}: GetTaskFallbackArgs): Promise<void> {
  const task = await client.getTask(taskId, { signal });
  // A tasks/get Task carries the answer in artifacts and/or its history
  // messages; extractParts(task) only reads artifacts, so fall back to the
  // history messages when artifacts are empty.
  let fallbackParts = extractParts(task);
  if (fallbackParts.length === 0) {
    const history = (task as { history?: unknown[] }).history ?? [];
    fallbackParts = history.flatMap((msg) => extractParts(msg));
  }
  if (fallbackParts.length === 0) return;
  setMessages((prev) =>
    prev.map((m) =>
      m.id === bubbleId
        ? { ...m, segments: appendPartsToSegments(m.segments, fallbackParts) }
        : m,
    ),
  );
}

// A resubscribe that errors at the tail of a turn is torn down shortly after:
// onTerminal triggers an immediate tasks refetch, activeTaskId clears, and the
// effect cleanup runs. Defer the error trailer behind this window so only a
// failure that outlives it surfaces; transient flaps clear the timer in cleanup
// and never render. Surfacing is therefore best-effort, not guaranteed.
const RESUBSCRIBE_ERROR_GRACE_MS = 1500;

export function useTaskResubscribe({
  activeTaskId,
  client,
  suspended,
  setMessages,
  onTerminal,
  progressRef,
  onSessionLost,
}: UseTaskResubscribeArgs): void {
  const onSessionLostRef = useRef(onSessionLost);
  useEffect(() => {
    onSessionLostRef.current = onSessionLost;
  }, [onSessionLost]);

  useEffect(() => {
    if (!activeTaskId || !client || suspended) return;
    const controller = new AbortController();
    const bubbleId = _liveBubbleId(activeTaskId);

    // Insert the live bubble before anything streams so the user gets a
    // "reconnecting…" affordance even if the first event takes a moment.
    setMessages((prev) =>
      prev.some((m) => m.id === bubbleId)
        ? prev
        : [
            ...prev,
            {
              id: bubbleId,
              role: "assistant",
              createdAt: Date.now(),
              segments: [],
              pending: true,
              // Tag with the task so the merge drops this live bubble once the
              // completed task lands in history (no duplicate).
              taskId: activeTaskId,
            },
          ],
    );

    let cancelled = false;
    let errorTimer: ReturnType<typeof setTimeout> | null = null;

    (async () => {
      try {
        const stream = client.resubscribeTask(activeTaskId, {
          signal: controller.signal,
        });
        for await (const event of stream) {
          if (cancelled) break;
          if (progressRef) progressRef.current++;
          const parts = extractParts(event);
          if (parts.length > 0) {
            setMessages((prev) =>
              prev.map((m) => {
                if (m.id !== bubbleId) return m;
                const segments: MessageSegment[] = appendPartsToSegments(
                  m.segments,
                  parts,
                );
                return { ...m, segments };
              }),
            );
          }
          if (isFinal(event)) break;
        }
      } catch (err) {
        const aborted =
          (err instanceof DOMException && err.name === "AbortError") ||
          controller.signal.aborted;
        if (!aborted && !cancelled) {
          if (isResubscribeInactiveError(err)) {
            // The SDK THROWS on a resubscribe error frame (it never yields the
            // error object). "Task not active here / already terminal" is
            // recoverable: fetch the finished task via tasks/get and render it
            // instead of surfacing a resubscribe-error trailer.
            try {
              await _renderViaGetTask({
                client,
                taskId: activeTaskId,
                bubbleId,
                signal: controller.signal,
                setMessages,
              });
            } catch {
              // getTask itself failed — fall through to the finally, which
              // clears the spinner and refreshes history.
            }
          } else if (isTransportError(err)) {
            setMessages((prev) =>
              prev.map((m) =>
                m.id === bubbleId ? { ...m, pending: false } : m,
              ),
            );
            onSessionLostRef.current?.();
          } else {
            const message = err instanceof Error ? err.message : String(err);
            errorTimer = setTimeout(() => {
              if (cancelled) return;
              setMessages((prev) =>
                prev.map((m) =>
                  m.id === bubbleId
                    ? {
                        ...m,
                        pending: false,
                        segments: [
                          ...m.segments,
                          {
                            kind: "text",
                            text: `\n\n_⚠️ resubscribe error: ${message}_`,
                          },
                        ],
                      }
                    : m,
                ),
              );
            }, RESUBSCRIBE_ERROR_GRACE_MS);
          }
        }
      } finally {
        if (cancelled) {
          // Unmount or activeTaskId switched mid-stream: drop the orphan.
          setMessages((prev) => prev.filter((m) => m.id !== bubbleId));
        } else {
          // Clear the spinner but leave the bubble in place; history refresh
          // will replace messages wholesale with the now-complete task, so
          // dropping it here would leave a blank gap during the refetch.
          setMessages((prev) =>
            prev.map((m) => (m.id === bubbleId ? { ...m, pending: false } : m)),
          );
          await onTerminal();
        }
      }
    })();

    return () => {
      cancelled = true;
      if (errorTimer) clearTimeout(errorTimer);
      controller.abort();
    };
    // progressRef is a stable ref object; including it cannot re-tap the SSE.
  }, [activeTaskId, client, suspended, setMessages, onTerminal, progressRef]);
}
