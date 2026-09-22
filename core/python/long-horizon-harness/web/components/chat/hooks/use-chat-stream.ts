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

import { useCallback, useEffect, useRef, useState } from "react";
import type { Part } from "@a2a-js/sdk";
import type { HorizonClient } from "@/lib/a2a-client";
import {
  dataPartDict,
  makeFileBytesPart,
  makeFileUrlPart,
} from "@/lib/a2a-part";
import { extractParts, extractTaskId, isFinal } from "@/lib/horizon-events";
import {
  appendPartsToSegments,
  resolveHitlSegments,
  resolveToolResults,
} from "@/lib/chat-segments";
import { isTransportError } from "@/lib/is-transport-error";
import type { ChatMessage, MessageSegment } from "../chat-shell";
import type { InputAttachment } from "../input-box";

export interface UseChatStreamArgs {
  client: HorizonClient | null;
  contextIdProp: string | undefined;
  // Fires when the assistant stream ends (success, error, or abort). The flag
  // is true iff any tool segment in the turn matched the FS-touching allowlist
  // — caller uses this to refresh the workspace tree only when needed.
  onTurnComplete?: (touchedFs: boolean) => void;
  // Fired when the stream fails with a transport-layer error (backend gone,
  // offline). Caller swaps the inline error trailer for a top-level banner.
  onSessionLost?: () => void;
  // Reports each A2A task id the live stream drives, as soon as it appears in
  // the stream. The caller records these so the lagging tasks poll never wakes
  // a resubscribe for a task this client already drove live.
  onLiveTaskId?: (taskId: string) => void;
  // Called on the first send of a brand-new chat (no contextId in the URL yet)
  // to lock the generated contextId into the URL. Must update the URL WITHOUT
  // remounting this component — a path change ('/' -> '/c') remounts under
  // TanStack Router and would abort the in-flight stream, so the caller updates
  // search params on the current route instead.
  onFirstSend?: (contextId: string) => void;
}

// Tools whose execution can change /workspace contents. Names must match the
// `name` field on function-call data parts emitted by the agent (see
// horizon/tools/*). Kept narrow so quiet turns don't trigger refetches; widen as
// new FS-touching tools land.
const FS_TOUCHING_TOOLS = new Set([
  "bash", // arbitrary shell — assume any invocation may touch the FS
  "write",
  "edit",
]);

export interface SendOptions {
  extraParts?: Part[];
  attachments?: InputAttachment[];
}

export interface ConfirmInput {
  callId: string | null;
  confirmed: boolean;
  payload: Record<string, unknown> | null;
}

export interface UseChatStreamResult {
  messages: ChatMessage[];
  setMessages: React.Dispatch<React.SetStateAction<ChatMessage[]>>;
  send: (text: string, opts?: SendOptions) => Promise<void>;
  confirm: (req: ConfirmInput | ConfirmInput[]) => Promise<void>;
  resend: (message: ChatMessage) => Promise<void>;
  editAndResend: (message: ChatMessage, newText: string) => Promise<void>;
  onStop: () => void;
  busy: boolean;
}

export function useChatStream({
  client,
  contextIdProp,
  onTurnComplete,
  onSessionLost,
  onLiveTaskId,
  onFirstSend,
}: UseChatStreamArgs): UseChatStreamResult {
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [busy, setBusy] = useState(false);
  const abortRef = useRef<AbortController | null>(null);
  // The A2A task id the in-flight stream is driving, so onStop can issue a real
  // server-side tasks/cancel (not just a client-side fetch abort).
  const liveTaskIdRef = useRef<string | null>(null);
  const busyRef = useRef(false);
  // Stash the latest callbacks in refs so consumeStream's identity stays
  // stable across renders — otherwise every parent re-render would re-create
  // the stream consumer.
  const onTurnCompleteRef = useRef(onTurnComplete);
  const onSessionLostRef = useRef(onSessionLost);
  const onLiveTaskIdRef = useRef(onLiveTaskId);
  useEffect(() => {
    onTurnCompleteRef.current = onTurnComplete;
  }, [onTurnComplete]);
  useEffect(() => {
    onSessionLostRef.current = onSessionLost;
  }, [onSessionLost]);
  useEffect(() => {
    onLiveTaskIdRef.current = onLiveTaskId;
  }, [onLiveTaskId]);

  useEffect(() => {
    busyRef.current = busy;
  }, [busy]);

  // A context switch (or any client rebuild) means the in-flight stream is
  // for a session we no longer care about — kill it so its `finally` block
  // doesn't flip busy/segments on the next session.
  useEffect(() => {
    return () => {
      abortRef.current?.abort();
      abortRef.current = null;
    };
  }, [client]);

  // Consume an A2A event stream into the given assistant message. Shared
  // between send (text turn) and confirm (continuation turn).
  const consumeStream = useCallback(
    async (
      assistantMsgId: string,
      stream: AsyncGenerator<unknown, void, void>,
      controller: AbortController,
      // Optional sink for tool_result parts seen on the stream. The confirm
      // continuation lands its resumed tool result in a fresh bubble with no
      // matching function_call, so appendPartsToSegments drops it — confirm()
      // collects them here to re-attach to the original spinning row.
      onToolResult?: (r: {
        callId: string | null;
        name: string;
        result: unknown;
      }) => void,
    ) => {
      let turnTouchedFs = false;
      try {
        for await (const event of stream) {
          const taskId = extractTaskId(event);
          if (taskId) {
            onLiveTaskIdRef.current?.(taskId);
            liveTaskIdRef.current = taskId;
            // Tag the optimistic assistant bubble with its task so the
            // history↔live merge can drop it once the canonical copy lands.
            setMessages((prev) =>
              prev.map((m) =>
                m.id === assistantMsgId && m.taskId !== taskId
                  ? { ...m, taskId }
                  : m,
              ),
            );
          }
          // Parts arrive deduplicated; feed each straight through —
          // appendPartsToSegments concatenates consecutive text deltas.
          const parts = extractParts(event);
          for (const p of parts) {
            if (p.kind === "tool" && FS_TOUCHING_TOOLS.has(p.name)) {
              turnTouchedFs = true;
            }
            if (p.kind === "tool_result") {
              onToolResult?.({
                callId: p.callId,
                name: p.name,
                result: p.result,
              });
            }
          }
          if (parts.length > 0) {
            setMessages((prev) =>
              prev.map((m) => {
                if (m.id !== assistantMsgId) return m;
                const segments = appendPartsToSegments(m.segments, parts);
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
        if (!aborted && isTransportError(err)) {
          setMessages((prev) =>
            prev.map((m) =>
              m.id === assistantMsgId ? { ...m, pending: false } : m,
            ),
          );
          onSessionLostRef.current?.();
        } else {
          const trailer = aborted
            ? "\n\n_⏹ stopped_"
            : `\n\n_⚠️ stream error: ${err instanceof Error ? err.message : String(err)}_`;
          setMessages((prev) =>
            prev.map((m) =>
              m.id === assistantMsgId
                ? {
                    ...m,
                    pending: false,
                    segments: [...m.segments, { kind: "text", text: trailer }],
                  }
                : m,
            ),
          );
        }
      } finally {
        setMessages((prev) =>
          prev.map((m) =>
            m.id === assistantMsgId ? { ...m, pending: false } : m,
          ),
        );
        setBusy(false);
        abortRef.current = null;
        liveTaskIdRef.current = null;
        onTurnCompleteRef.current?.(turnTouchedFs);
      }
    },
    [],
  );

  const send = useCallback(
    async (text: string, opts?: SendOptions) => {
      const attachments = opts?.attachments ?? [];
      if (!client || busyRef.current) return;
      // Defense-in-depth: if the busyRef guard ever races, kill the prior stream
      // before opening a new one.
      abortRef.current?.abort();
      // A UI action (e.g. Export) sends parts and no prose; only a wholly
      // empty message is a no-op.
      const extra = opts?.extraParts ?? [];
      if (!text.trim() && attachments.length === 0 && extra.length === 0)
        return;

      // First send on a brand-new chat: lock the generated contextId into the
      // URL for bookmarkability. The caller updates search params on the current
      // route (not a path change) so this component is not remounted — a remount
      // would abort the in-flight stream and the freshly added pending bubble.
      if (!contextIdProp) {
        onFirstSend?.(client.contextId);
      }

      // Read each attachment into base64 before we commit anything to the
      // bubble — if a file fails to read we surface an error and abort.
      let attachmentParts: Part[] = [];
      let userFileSegments: MessageSegment[] = [];
      if (attachments.length > 0) {
        try {
          const encoded = await Promise.all(
            attachments.map(async (a) => ({
              bytes: await fileToBase64(a.file),
              mimeType: a.mimeType,
              name: a.name,
            })),
          );
          attachmentParts = encoded.map((e) =>
            makeFileBytesPart(e.bytes, {
              filename: e.name,
              mediaType: e.mimeType,
            }),
          );
          userFileSegments = encoded.map((e) => ({
            kind: "file",
            file: { bytes: e.bytes, mimeType: e.mimeType, name: e.name },
          }));
        } catch (err) {
          const msg = err instanceof Error ? err.message : String(err);
          setMessages((prev) => [
            ...prev,
            {
              id: crypto.randomUUID(),
              role: "system",
              createdAt: Date.now(),
              segments: [
                { kind: "text", text: `⚠️ Failed to read attachment: ${msg}` },
              ],
            },
          ]);
          return;
        }
      }

      const userMsgId = crypto.randomUUID();
      const assistantMsgId = crypto.randomUUID();
      const userSegments: MessageSegment[] = [...userFileSegments];
      // Mirror caller-supplied data parts into the optimistic bubble, so a UI
      // payload (e.g. a highlighted selection) renders before history catches
      // up rather than popping in on the refetch.
      for (const p of opts?.extraParts ?? []) {
        const data = dataPartDict(p);
        if (data) userSegments.push({ kind: "data", mime: undefined, data });
      }
      if (text.trim()) userSegments.push({ kind: "text", text });
      setMessages((prev) => [
        ...prev,
        {
          id: userMsgId,
          role: "user",
          createdAt: Date.now(),
          segments: userSegments,
        },
        {
          id: assistantMsgId,
          role: "assistant",
          createdAt: Date.now(),
          segments: [],
          pending: true,
        },
      ]);
      setBusy(true);

      const controller = new AbortController();
      abortRef.current = controller;

      const extraParts: Part[] = [
        ...attachmentParts,
        ...(opts?.extraParts ?? []),
      ];

      const stream = client.sendStream(text, {
        signal: controller.signal,
        // Echo our optimistic user-bubble id to the server so the same id comes
        // back in history — the merge then dedups the user bubble instead of
        // rendering it twice.
        messageId: userMsgId,
        extraParts: extraParts.length > 0 ? extraParts : undefined,
      });
      await consumeStream(assistantMsgId, stream, controller);
    },
    [client, consumeStream, contextIdProp, onFirstSend],
  );

  const confirm = useCallback(
    async (req: ConfirmInput | ConfirmInput[]) => {
      if (!client || busyRef.current) return;

      if (Array.isArray(req)) {
        const items = req.filter((r) => r.callId);
        if (items.length === 0) return;
        abortRef.current?.abort();
        setMessages((prev) =>
          items.reduce((acc, r) => resolveHitlSegments(acc, r), prev),
        );

        const assistantMsgId = crypto.randomUUID();
        setMessages((prev) => [
          ...prev,
          {
            id: assistantMsgId,
            role: "assistant",
            createdAt: Date.now(),
            segments: [],
            pending: true,
          },
        ]);
        setBusy(true);

        const controller = new AbortController();
        abortRef.current = controller;
        const stream = client.sendConfirmations(
          items.map((r) => ({
            callId: r.callId as string,
            confirmed: r.confirmed,
            payload: r.payload,
          })),
          { signal: controller.signal },
        );
        const toolResults: {
          callId: string | null;
          name: string;
          result: unknown;
        }[] = [];
        await consumeStream(assistantMsgId, stream, controller, (r) =>
          toolResults.push(r),
        );
        if (toolResults.length > 0)
          setMessages((prev) => resolveToolResults(prev, toolResults));
        return;
      }

      if (!req.callId) return;
      abortRef.current?.abort();

      // Flip the originating clarify/confirmation card to its resolved view
      // the instant the user answers, even while the continuation streams.
      setMessages((prev) => resolveHitlSegments(prev, req));

      const assistantMsgId = crypto.randomUUID();
      setMessages((prev) => [
        ...prev,
        {
          id: assistantMsgId,
          role: "assistant",
          createdAt: Date.now(),
          segments: [],
          pending: true,
        },
      ]);
      setBusy(true);

      const controller = new AbortController();
      abortRef.current = controller;
      const stream = client.sendConfirmation({
        callId: req.callId,
        confirmed: req.confirmed,
        payload: req.payload,
        signal: controller.signal,
      });
      // The continuation streams the resumed gated tool's function_response into
      // a fresh bubble with no matching function_call, so it never attaches to
      // the original spinning row. Collect those results and re-attach by callId.
      const toolResults: {
        callId: string | null;
        name: string;
        result: unknown;
      }[] = [];
      await consumeStream(assistantMsgId, stream, controller, (r) =>
        toolResults.push(r),
      );
      if (toolResults.length > 0) {
        setMessages((prev) => resolveToolResults(prev, toolResults));
      }
    },
    [client, consumeStream],
  );

  // Pull the original message's text + file segments and re-send as a new
  // turn. File segments already carry base64 bytes (FilePart shape on the
  // wire), so we can feed them straight through `extraParts` — no need to
  // round-trip through `InputAttachment`/`File`.
  const buildResendPayload = useCallback(
    (message: ChatMessage): { text: string; extraParts: Part[] } => {
      const textChunks: string[] = [];
      const extraParts: Part[] = [];
      for (const seg of message.segments) {
        if (seg.kind === "text") {
          textChunks.push(seg.text);
        } else if (seg.kind === "file") {
          const part = fileSegmentToPart(seg.file);
          if (part) extraParts.push(part);
        }
      }
      return { text: textChunks.join("\n").trim(), extraParts };
    },
    [],
  );

  const resend = useCallback(
    async (message: ChatMessage) => {
      const { text, extraParts } = buildResendPayload(message);
      if (!text && extraParts.length === 0) return;
      await send(text, {
        extraParts: extraParts.length > 0 ? extraParts : undefined,
      });
    },
    [buildResendPayload, send],
  );

  const editAndResend = useCallback(
    async (message: ChatMessage, newText: string) => {
      const trimmed = newText.trim();
      const { extraParts } = buildResendPayload(message);
      if (!trimmed && extraParts.length === 0) return;
      await send(trimmed, {
        extraParts: extraParts.length > 0 ? extraParts : undefined,
      });
    },
    [buildResendPayload, send],
  );

  const onStop = useCallback(() => {
    // Real server-side cancel so the interrupted turn actually stops and can't
    // re-materialize out of order via history. Fire-and-forget: a cancel that
    // races a natural completion rejects with TaskNotCancelableError, a no-op
    // for us. The local abort still tears down the SSE immediately.
    const tid = liveTaskIdRef.current;
    if (tid && client) void client.cancelTask(tid).catch(() => {});
    abortRef.current?.abort();
  }, [client]);

  return {
    messages,
    setMessages,
    send,
    confirm,
    resend,
    editAndResend,
    onStop,
    busy,
  };
}

// Build an A2A FilePart from a stored file segment. Returns null if the
// segment has neither bytes nor uri — nothing to send.
function fileSegmentToPart(file: {
  bytes?: string;
  uri?: string;
  mimeType?: string;
  name?: string;
}): Part | null {
  if (file.bytes) {
    return makeFileBytesPart(file.bytes, {
      filename: file.name,
      mediaType: file.mimeType,
    });
  }
  if (file.uri) {
    return makeFileUrlPart(file.uri, {
      filename: file.name,
      mediaType: file.mimeType,
    });
  }
  return null;
}

// Read a File as a raw base64 string (no `data:...;base64,` prefix), the
// shape A2A's FileWithBytes expects on the wire.
function fileToBase64(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onerror = () =>
      reject(reader.error ?? new Error("file read failed"));
    reader.onload = () => {
      const result = reader.result;
      if (typeof result !== "string") {
        reject(new Error("unexpected FileReader result"));
        return;
      }
      const comma = result.indexOf(",");
      resolve(comma >= 0 ? result.slice(comma + 1) : result);
    };
    reader.readAsDataURL(file);
  });
}
