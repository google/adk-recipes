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

import {
  type DragEvent,
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import { useNavigate } from "@tanstack/react-router";
import { createLhaClient, type HorizonClient } from "@/lib/a2a-client";
import { setChatWindow, useLhaSessions } from "@/lib/horizon-sessions";
import { takePendingWindow } from "@/lib/pending-window";

import { MessageList } from "./message-list";
import { InputBox, type InputAttachment } from "./input-box";
import { SidebarRail } from "@/components/nav/sidebar-rail";

import { ChatListSidebar } from "./chat-list-sidebar";
import {
  ResizablePanelGroup,
  ResizablePanel,
  ResizableHandle,
} from "@/components/ui/resizable";
import { ArtifactViewer } from "./artifact-viewer/artifact-viewer";
import {
  ViewerProvider,
  type PendingSelection,
} from "./artifact-viewer/viewer-context";
import { anchorLabel } from "@/lib/selection-anchor";
import type { Part } from "@a2a-js/sdk";
import { dataPartDict, makeDataPart } from "@/lib/a2a-part";
import { newId } from "@/lib/new-id";
import {
  SELECTION_DATA_KIND,
  isSelectionPayload,
  type SelectionPayload,
} from "./workspace-href";
import { useMediaQuery } from "@/lib/use-media-query";
import type { ImperativePanelHandle } from "react-resizable-panels";
import {
  ChatActionButtons,
  ChatDeleteError,
  ChatTitleInput,
  useChatTitleControls,
} from "./chat-title-controls";
import { GuardrailBanner } from "./guardrail-banner";

import { SessionLostBanner } from "./session-lost-banner";
import { isTransportError } from "@/lib/is-transport-error";
import { Button } from "@/components/ui/button";
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
  SheetTrigger,
} from "@/components/ui/sheet";
import {
  Brain,
  CalendarClock,
  LineChart,
  ListFilter,
  Play,
  Newspaper,
  PanelLeft,
  PanelRight,
  PanelRightClose,
  PanelRightOpen,
  type LucideIcon,
} from "lucide-react";

import { textStyle } from "@/lib/typography";
import { cn, formatBytes } from "@/lib/utils";
import { rememberTask, rememberConversation } from "@/lib/aqua-conversations";
import { useLhaTasks } from "@/lib/horizon-tasks";
import {
  forgetLastChat,
  readLastChat,
  rememberLastChat,
} from "@/lib/last-chat";
import { mergeHistoryWithLive } from "@/lib/merge-history-with-live";
import { prefetchChatHistory } from "@/lib/prefetch-chat-history";
import { useChatStream } from "./hooks/use-chat-stream";
import { useMessageQueue } from "./hooks/use-message-queue";
import { useTaskHistory } from "./hooks/use-task-history";
import { useTaskResubscribe } from "./hooks/use-task-resubscribe";
import { ConfirmationProvider } from "./confirmation-context";
import { BusyProvider } from "./busy-context";
import { NowProvider } from "@/lib/now-context";
import { DormantActionsGate } from "./dormant-actions";
import { Wordmark } from "@/components/brand/wordmark";
import { CommandPalette } from "./command-palette";
import { FindBar } from "./find-bar";
import { ImageLightboxProvider } from "./image-lightbox";
import { focusComposer } from "@/lib/composer-focus";
import { downloadConversation } from "@/lib/export-conversation";
import { notify } from "@/lib/toast";
import { loadDraft, saveDraft } from "@/lib/draft-store";
import { compressImageFile } from "@/lib/compress-image";

export interface ChatMessage {
  id: string;
  role: "user" | "assistant" | "system";
  segments: MessageSegment[];
  pending?: boolean;
  createdAt: number;
  // The A2A task this bubble belongs to. Set on optimistic/live assistant
  // bubbles (once the stream reveals the task id) and on every history bubble,
  // so the history↔live merge can drop an optimistic bubble once its task's
  // canonical copy lands in history.
  taskId?: string;
}

export type MessageSegment =
  | { kind: "text"; text: string }
  | { kind: "data"; mime: string | undefined; data: unknown }
  | {
      kind: "tool";
      callId: string | null;
      name: string;
      args: Record<string, unknown> | null;
      // Filled in when the matching function_response arrives. `undefined`
      // means still in flight; `null` is a legal tool return value.
      result?: unknown;
      hasResult?: boolean;
    }
  | {
      kind: "clarify";
      callId: string | null;
      question: string;
      choices: string[];
      // Present once the user has responded ⇒ the card renders a resolved,
      // non-interactive summary. `text` is the chosen/typed answer (null for a
      // bare decline); `confirmed` distinguishes answer from decline.
      answer?: { confirmed: boolean; text: string | null };
    }
  | {
      kind: "confirmation";
      callId: string | null;
      hint: string;
      originalCall: {
        name: string;
        args: Record<string, unknown> | null;
      } | null;
      payload: Record<string, unknown> | null;
      answer?: { confirmed: boolean; text: string | null };
    }
  | {
      kind: "file";
      file: {
        bytes?: string;
        uri?: string;
        mimeType?: string;
        name?: string;
      };
    };

export interface ChatShellProps {
  contextId?: string;
  /** A question typed on the dashboard, sent once the client is up. Lets the
   *  homepage keep an ask bar without owning a second chat implementation. */
  initialMessage?: string;
}

const MAX_FILE_BYTES = 20 * 1024 * 1024;
const IMAGE_PREFIX = "image/";

export function ChatShell({
  contextId: contextIdProp,
  initialMessage,
}: ChatShellProps = {}) {
  const [client, setClient] = useState<HorizonClient | null>(null);
  // The contextId the current client was booted for. Lets the boot effect skip
  // a rebuild when the first-send URL lock flips contextIdProp from undefined to
  // the id the live client already owns (so the in-flight stream survives).
  const bootedContextIdRef = useRef<string | null>(null);
  const [bootError, setBootError] = useState<string | null>(null);
  const [sessionLost, setSessionLost] = useState(false);

  const [panelsOpen, setPanelsOpen] = useState(false);
  const [sidebarOpen, setSidebarOpen] = useState(false);
  const [rightCollapsed, setRightCollapsed] = useState(true);
  const rightPanelRef = useRef<ImperativePanelHandle>(null);
  const isDesktop = useMediaQuery("(min-width: 768px)");
  const handleArtifactOpen = useCallback(() => {
    // Desktop: the right panel is a resizable pane (expand it if collapsed).
    // Mobile: it lives in a Sheet. Never set panelsOpen on desktop or the
    // Sheet would pop OVER the panel.
    if (isDesktop) rightPanelRef.current?.expand();
    else setPanelsOpen(true);
  }, [isDesktop]);
  const toggleRightPanel = useCallback(() => {
    const panel = rightPanelRef.current;
    if (!panel) return;
    if (panel.isCollapsed()) panel.expand();
    else panel.collapse();
  }, []);
  useEffect(() => {
    setRightCollapsed(Boolean(rightPanelRef.current?.isCollapsed()));
  }, []);
  const [findOpen, setFindOpen] = useState(false);
  const [findHighlightId, setFindHighlightId] = useState<string | null>(null);
  const [findFocusSignal, setFindFocusSignal] = useState(0);
  const findOpenRef = useRef(false);
  findOpenRef.current = findOpen;
  const [attachments, setAttachments] = useState<InputAttachment[]>([]);
  const [attachmentError, setAttachmentError] = useState<string | null>(null);
  const [draft, setDraft] = useState(() => loadDraft(contextIdProp));
  const [dragOver, setDragOver] = useState(false);
  // Bumped by "New chat" when we're already on `/` — without a URL change
  // TanStack Router won't remount us, so we re-key the boot effect manually to
  // regenerate the contextId and wipe the message list.
  const [resetKey, setResetKey] = useState(0);
  // Hide the dormant quick-action row once the user acts on it, before the
  // sessions poll refreshes lastUpdated. Reset when switching chats so another
  // dormant chat re-offers the buttons.
  const [dormantActionsDismissed, setDormantActionsDismissed] = useState(false);
  // Text highlighted in the artifact panel, prepended to the next message as
  // an `[Editing …]` block so the agent can resolve it with `patch`. Owned
  // here because this component renders ViewerProvider and so can't read its
  // context; the viewer reports upward via onSelectionChange.
  const [pendingSelection, setPendingSelection] =
    useState<PendingSelection | null>(null);
  // Set when the panel's Export starts a turn, so its result claims the panel
  // rather than waiting for an empty slot. Cleared once that turn is over.
  const dragDepthRef = useRef(0);
  const navigate = useNavigate();
  const {
    data: sessions,
    isLoading: sessionsLoading,
    refresh: refreshSessions,
  } = useLhaSessions();

  const {
    tasks,
    activeTaskId,
    refresh: refreshTasks,
  } = useLhaTasks(contextIdProp ?? null);

  // Shared between resubscribe (writer) and history (reader) so a reattached
  // turn's frames drive a history refresh without restarting the subscription.
  const progressRef = useRef(0);

  const { historyMessages, historyLoading } = useTaskHistory({
    contextId: contextIdProp ?? null,
    tasks,
    client,
    progressRef,
  });

  const handleSessionLost = useCallback(() => {
    setSessionLost(true);
  }, []);

  // Lock a brand-new chat's contextId into the URL on first send. Update search
  // params on the current ('/') route — NOT a path change to '/c' — so this
  // component is not remounted mid-stream (a remount aborts the live stream).
  // The contextId is the one the live client already owns, so the boot effect's
  // guard skips rebuilding the client when contextIdProp flips from undefined
  // to this id. Sidebar/command-palette links to /c?id=… continue to work.
  const lockContextUrl = useCallback(
    (cid: string) => {
      navigate({ to: ".", search: { id: cid }, replace: true });
    },
    [navigate],
  );

  // Task ids the live stream drove this session. Resubscribe must skip them:
  // the tasks poll lags the live stream, so right after a turn it can still
  // report the just-finished task as active and wake a duplicate resubscribe.
  const liveTaskIdsRef = useRef<Set<string>>(new Set());
  const recordLiveTaskId = useCallback(
    (id: string) => {
      liveTaskIdsRef.current.add(id);
      // Persist it too: this is the only record that the conversation had this
      // turn, and without it reopening the chat replays nothing. Filed under
      // the client's context rather than the URL's: after a switch the URL
      // already names the next chat while this stream is still being torn down.
      if (client) rememberTask(client.contextId, id);
    },
    [client],
  );
  // biome-ignore lint/correctness/useExhaustiveDependencies(contextIdProp): live task IDs are scoped to the active chat.
  useEffect(() => {
    liveTaskIdsRef.current = new Set();
  }, [contextIdProp]);

  // biome-ignore lint/correctness/useExhaustiveDependencies(contextIdProp): dismissal state is scoped to the active chat.
  useEffect(() => {
    setDormantActionsDismissed(false);
  }, [contextIdProp]);

  // Warm a hovered chat's history using the active client (getTask is
  // context-agnostic) so clicking it renders from cache instantly.
  const handlePrefetchChat = useCallback(
    (id: string) => {
      void prefetchChatHistory(id, client);
    },
    [client],
  );

  const {
    messages,
    setMessages,
    send,
    confirm,
    resend,
    editAndResend,
    onStop,
    busy,
  } = useChatStream({
    client,
    contextIdProp,
    onTurnComplete: (touchedFs) => {
      if (touchedFs) {
        // file / artifact state updated
      }
      // Pull the now-complete task into history.
      void refreshTasks();
    },
    onSessionLost: handleSessionLost,
    onLiveTaskId: recordLiveTaskId,
    onFirstSend: lockContextUrl,
  });

  const trimmedInitialMessage = initialMessage?.trim() || undefined;
  const initialTurnBooting =
    Boolean(trimmedInitialMessage) &&
    messages.length === 0 &&
    !bootError &&
    !sessionLost;
  const sentInitialRef = useRef<string | null>(null);
  const initialTurnIdsRef = useRef<{
    prompt: string;
    user: string;
    assistant: string;
    createdAt: number;
  } | null>(null);
  if (
    trimmedInitialMessage &&
    initialTurnIdsRef.current?.prompt !== trimmedInitialMessage
  ) {
    initialTurnIdsRef.current = {
      prompt: trimmedInitialMessage,
      user: newId(),
      assistant: newId(),
      createdAt: Date.now(),
    };
  }

  // Must stay after useChatStream: its busyRef-reset effect has to run before
  // this hook's flush effect, or the flush's send() would hit the busy guard.
  // `busy` only covers a turn this tab sent. A turn reattached after a reopen
  // is equally in flight, and sending into it starts a second concurrent turn
  // against the same session. Composer-facing state must use this instead.
  const turnActive = busy || activeTaskId !== null || initialTurnBooting;

  const { queue, enqueue, removeQueued, clearQueue } = useMessageQueue({
    busy: turnActive,
    send,
  });

  useTaskResubscribe({
    activeTaskId,
    client,
    suspended:
      busy ||
      (activeTaskId !== null && liveTaskIdsRef.current.has(activeTaskId)),
    setMessages,
    onTerminal: refreshTasks,
    onSessionLost: handleSessionLost,
    progressRef,
  });

  const confirmationSender = useCallback(
    (
      req:
        | {
            callId: string | null;
            confirmed: boolean;
            payload: Record<string, unknown> | null;
          }
        | {
            callId: string | null;
            confirmed: boolean;
            payload: Record<string, unknown> | null;
          }[],
    ) => {
      void confirm(req);
    },
    [confirm],
  );

  const handleFindActiveMatch = useCallback((id: string | null) => {
    setFindHighlightId(id);
    if (!id || typeof document === "undefined") return;
    document
      .querySelector(`[data-msg-id="${CSS.escape(id)}"]`)
      ?.scrollIntoView({ block: "center", behavior: "smooth" });
  }, []);

  const handleFindClose = useCallback(() => {
    setFindOpen(false);
    setFindHighlightId(null);
  }, []);

  // Cold-load on `/` with a stored contextId → rehydrate by routing to
  // /c?id=<stored>. The page remounts with contextIdProp set, useLhaTasks
  // starts polling, and any in-flight task gets auto-resubscribed.
  // Skip when trimmedInitialMessage (?q=...) is present so "Diagnose in chat"
  // from an insight starts a fresh conversation instead of hijacking into the
  // old one.
  useEffect(() => {
    if (contextIdProp || trimmedInitialMessage) return;
    const stored = readLastChat();
    if (!stored) return;
    navigate({ to: "/c", search: { id: stored }, replace: true });
  }, [contextIdProp, trimmedInitialMessage, navigate]);

  // A new chat's id reaches the URL with its first send, so an unsent chat,
  // which has nothing to return to, is never the one a bare /c reopens.
  useEffect(() => {
    if (contextIdProp) rememberLastChat(contextIdProp);
  }, [contextIdProp]);

  // Boot or rebuild the A2A client whenever the URL contextId changes.
  // No prop → generate a fresh contextId for a new chat; on first send we lock
  // ?id={contextId} into the URL so it becomes bookmarkable.
  // biome-ignore lint/correctness/useExhaustiveDependencies(resetKey): clicking New chat on an unsent chat bumps resetKey to boot a fresh client.
  useEffect(() => {
    const desired = contextIdProp ?? null;
    // First send locks the URL to the contextId the live client already owns
    // (undefined -> that id). Skip the rebuild so the in-flight stream and the
    // pending bubbles survive the URL change.
    if (desired && bootedContextIdRef.current === desired) return;

    let cancelled = false;
    const bootAbort = new AbortController();
    // The client is dropped here, so it does not own that id. Left stamped,
    // a return to the id (Back, or the cold-load redirect to the last chat)
    // would take the early return above and never build a client again.
    bootedContextIdRef.current = null;
    setClient(null);
    setBootError(null);
    setSessionLost(false);
    setMessages([]);
    setDraft(loadDraft(contextIdProp));
    clearQueue();

    const contextId = contextIdProp ?? newId();

    createLhaClient({ contextId, signal: bootAbort.signal })
      .then((c) => {
        if (cancelled) return;
        // Stamp only once the client exists. Stamping before the await let an
        // aborted boot (StrictMode's mount->cleanup->mount) claim the id, so the
        // re-run returned early and no client was ever built.
        bootedContextIdRef.current = contextId;
        setClient(c);
      })
      .catch((err) => {
        if (cancelled) return;
        // Boot teardown aborts the retry loop — not a real failure.
        if (err instanceof DOMException && err.name === "AbortError") return;
        // eslint-disable-next-line no-console
        console.error("[horizon-boot] client init failed", err);
        if (isTransportError(err)) {
          setSessionLost(true);
        } else {
          setBootError(err instanceof Error ? err.message : String(err));
        }
      });
    return () => {
      cancelled = true;
      bootAbort.abort();
    };
  }, [contextIdProp, resetKey, setMessages, clearQueue]);

  const onNewChat = useCallback(() => {
    sentInitialRef.current = null;
    initialTurnIdsRef.current = null;
    forgetLastChat();
    if (contextIdProp || trimmedInitialMessage) {
      // `search: {}` matters: without it the `?id=` or `?q=` of the current
      // conversation survives the navigation and it reopens instead of
      // starting a new one.
      navigate({ to: "/c", search: {} });
    } else {
      setResetKey((k) => k + 1);
      setAttachments([]);
      setAttachmentError(null);
    }
  }, [contextIdProp, trimmedInitialMessage, navigate]);

  const [paletteOpen, setPaletteOpen] = useState(false);
  const paletteOpenRef = useRef(false);
  paletteOpenRef.current = paletteOpen;
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const meta = e.metaKey || e.ctrlKey;
      // Don't hijack typing or composing.
      const target = e.target as HTMLElement | null;
      const inEditable =
        !!target &&
        (target.tagName === "INPUT" ||
          target.tagName === "TEXTAREA" ||
          target.isContentEditable);

      if (meta && e.key.toLowerCase() === "k") {
        e.preventDefault();
        setPaletteOpen((v) => !v);
        return;
      }
      if (meta && e.key.toLowerCase() === "f") {
        e.preventDefault();
        setFindOpen(true);
        setFindFocusSignal((n) => n + 1);
        return;
      }
      // Close find on Escape even when focus has left the find bar. But if a
      // modal dialog is open (image lightbox, command palette), let it consume
      // the Escape first — otherwise one keystroke would close the dialog and
      // clear find together.
      if (e.key === "Escape" && findOpenRef.current) {
        if (document.querySelector('[role="dialog"][data-state="open"]'))
          return;
        e.preventDefault();
        setFindOpen(false);
        setFindHighlightId(null);
        return;
      }
      if (meta && e.key.toLowerCase() === "n" && !inEditable) {
        e.preventDefault();
        onNewChat();
        return;
      }
      if (meta && e.key.toLowerCase() === "l") {
        e.preventDefault();
        focusComposer();
        return;
      }
      if (e.key === "/" && !meta && !inEditable && !paletteOpenRef.current) {
        e.preventDefault();
        focusComposer();
        return;
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onNewChat]);

  // Merge replayed history into the live message stream once per fetch.
  // When a resubscribe is in flight, preserve its live bubble — otherwise the
  // wholesale replace clobbers the events being appended by useTaskResubscribe.
  // While a live send/confirm stream owns the turn (busy), skip the merge so a
  // mid-turn history refetch can't clobber the bubble the send is streaming
  // into. `busy` is intentionally read from the closure (not a dep): the merge
  // then runs on the next history refetch after the turn ends — by which point
  // history includes the completed turn — rather than firing on the busy→idle
  // transition with still-stale history.
  // biome-ignore lint/correctness/useExhaustiveDependencies: busy is read from the closure on purpose (see above), and activeTaskId triggers a merge when a turn settles.
  useEffect(() => {
    if (!historyMessages || busy) return;
    setMessages((prev) => mergeHistoryWithLive(prev, historyMessages));
  }, [historyMessages, activeTaskId, setMessages]);

  const addFiles = useCallback((files: File[]) => {
    if (files.length === 0) return;
    void (async () => {
      const processed = await Promise.all(files.map(compressImageFile));
      const rejected: string[] = [];
      const next: InputAttachment[] = [];
      for (const f of processed) {
        if (f.size > MAX_FILE_BYTES) {
          rejected.push(`${f.name || "file"} (${formatBytes(f.size)})`);
          continue;
        }
        const mime = f.type || "application/octet-stream";
        const previewUrl = mime.startsWith(IMAGE_PREFIX)
          ? URL.createObjectURL(f)
          : null;
        next.push({
          id: `${Date.now()}-${Math.random().toString(36).slice(2, 8)}`,
          name: f.name || `pasted-${mime.split("/")[1] ?? "file"}`,
          mimeType: mime,
          file: f,
          previewUrl,
        });
      }
      if (rejected.length) {
        setAttachmentError(
          `Too large (max ${formatBytes(MAX_FILE_BYTES)}): ${rejected.join(", ")}`,
        );
      } else {
        setAttachmentError(null);
      }
      if (next.length) {
        setAttachments((prev) => [...prev, ...next]);
      }
    })();
  }, []);

  const removeAttachment = useCallback((id: string) => {
    setAttachments((prev) => {
      const target = prev.find((a) => a.id === id);
      if (target?.previewUrl) URL.revokeObjectURL(target.previewUrl);
      return prev.filter((a) => a.id !== id);
    });
  }, []);

  const attachmentsRef = useRef(attachments);
  useEffect(() => {
    attachmentsRef.current = attachments;
  }, [attachments]);
  useEffect(() => {
    return () => {
      attachmentsRef.current.forEach((a) => {
        if (a.previewUrl) URL.revokeObjectURL(a.previewUrl);
      });
    };
  }, []);

  // Persist the draft synchronously on each keystroke (keyed by the current
  // contextId) so switching chats / reloading never loses typed text. Clearing
  // on send flows through here too (InputBox calls onValueChange("")).
  const onDraftChange = useCallback(
    (v: string) => {
      setDraft(v);
      saveDraft(contextIdProp, v);
    },
    [contextIdProp],
  );

  // biome-ignore lint/correctness/useExhaustiveDependencies(contextIdProp): contextIdProp is only a fallback while no client exists, and the client dep rebuilds this callback when the chat switches.
  const handleSend = useCallback(
    (
      text: string,
      presetIds?: { user: string; assistant: string; createdAt: number },
    ) => {
      // The sidebar's list is client-side, so a conversation only exists once
      // something is sent in it. Recorded here rather than on client boot, so
      // opening the app and typing nothing does not litter the list.
      const activeCid = client?.contextId ?? contextIdProp;
      if (activeCid) rememberConversation(activeCid, text);
      // Built before the busy check so a message queued mid-turn keeps its
      // selection and file references; flushing text alone would send the
      // instruction without the span it refers to.
      const queuedParts: Part[] = [];
      if (pendingSelection) {
        const a = pendingSelection.anchor;
        queuedParts.push(
          makeDataPart({
            lha_kind: SELECTION_DATA_KIND,
            path: pendingSelection.path,
            start: a.start,
            end: a.end,
            start_line: a.startLine,
            end_line: a.endLine,
            snippet: a.snippet,
          } satisfies SelectionPayload),
        );
      }
      if (busy || activeTaskId !== null) {
        enqueue(text, queuedParts);
        if (queuedParts.length > 0) {
          setPendingSelection(null);
        }
        return;
      }
      const sending = attachments;
      setAttachments([]);
      setAttachmentError(null);
      const outgoing = text;
      if (pendingSelection) setPendingSelection(null);
      const outgoingParts = queuedParts;
      // A project-chat seeds its workspace_window before the first turn so the
      // agent's file ops default into /workspace/<project>. Best-effort: a seed
      // failure must never swallow the user's message.
      const cid = client?.contextId;
      const dirs =
        cid && messages.length === 0 ? takePendingWindow(cid) : undefined;
      const sendOpts = {
        attachments: sending,
        extraParts: outgoingParts.length > 0 ? outgoingParts : undefined,
        userMsgId: presetIds?.user,
        assistantMsgId: presetIds?.assistant,
        createdAt: presetIds?.createdAt,
      };
      if (cid && dirs && dirs.length > 0) {
        void setChatWindow(cid, dirs).finally(() => send(outgoing, sendOpts));
      } else {
        void send(outgoing, sendOpts);
      }
    },
    [
      busy,
      activeTaskId,
      enqueue,
      attachments,
      pendingSelection,
      send,
      client,
      messages.length,
    ],
  );

  const handleStop = useCallback(() => {
    setDraft((d) =>
      [d, ...queue.map((q) => q.text)]
        .map((s) => s.trim())
        .filter(Boolean)
        .join("\n\n"),
    );
    // The chips were cleared when these were queued, so without this the text
    // comes back to the composer and its selection or file references are
    // gone — the next send would carry the instruction and nothing to apply
    // it to.
    const queuedPayloads = queue
      .flatMap((q) => q.parts)
      .map((p): unknown => dataPartDict(p))
      .filter((d) => d !== null);
    const restoredSelection = queuedPayloads.filter(isSelectionPayload).pop();
    if (restoredSelection) {
      setPendingSelection({
        path: restoredSelection.path,
        anchor: {
          start: restoredSelection.start,
          end: restoredSelection.end,
          startLine: restoredSelection.start_line,
          endLine: restoredSelection.end_line,
          snippet: restoredSelection.snippet,
        },
      });
    }
    clearQueue();
    // onStop cancels via the live send's own task id, which a reattached turn
    // doesn't have — cancel that one by id instead.
    if (busy) {
      onStop();
    } else if (activeTaskId && client) {
      void client
        .cancelTask(activeTaskId)
        .catch(() => {})
        .then(() => refreshTasks());
    }
  }, [queue, clearQueue, onStop, busy, activeTaskId, client, refreshTasks]);

  const onDragEnter = (e: DragEvent<HTMLElement>) => {
    if (!hasFileDrag(e)) return;
    dragDepthRef.current += 1;
    setDragOver(true);
  };
  const onDragLeave = (e: DragEvent<HTMLElement>) => {
    if (!hasFileDrag(e)) return;
    dragDepthRef.current = Math.max(0, dragDepthRef.current - 1);
    if (dragDepthRef.current === 0) setDragOver(false);
  };
  const onDragOver = (e: DragEvent<HTMLElement>) => {
    if (!hasFileDrag(e)) return;
    e.preventDefault();
  };
  const onDrop = (e: DragEvent<HTMLElement>) => {
    if (!hasFileDrag(e)) return;
    e.preventDefault();
    dragDepthRef.current = 0;
    setDragOver(false);
    const files = Array.from(e.dataTransfer.files ?? []);
    if (files.length) addFiles(files);
  };

  // Send the dashboard's question once, when the client is up. The ref, keyed
  // by trimmedInitialMessage, is the guard: the effect reruns on every client
  // change and a second send of the same prompt would duplicate the turn.
  // Only a client booted for this chat will do. On the render that brings a
  // new ?q= from an open chat, `client` is still that chat's (the boot effect
  // above has just unstamped it), and sending through it would post the
  // question there. A turn still winding down there would queue the question
  // instead, and the queue does not flush while the question is booting.
  useEffect(() => {
    if (!trimmedInitialMessage) {
      sentInitialRef.current = null;
      initialTurnIdsRef.current = null;
      return;
    }
    if (
      !client ||
      client.contextId !== bootedContextIdRef.current ||
      busy ||
      sentInitialRef.current === trimmedInitialMessage
    ) {
      return;
    }
    sentInitialRef.current = trimmedInitialMessage;
    handleSend(trimmedInitialMessage, initialTurnIdsRef.current ?? undefined);
  }, [trimmedInitialMessage, client, busy, handleSend]);

  const status = useMemo(() => {
    if (bootError || sessionLost)
      return { dot: "destructive" as const, label: "Disconnected" };
    if (!client || historyLoading)
      return {
        dot: "muted" as const,
        label: historyLoading ? "Loading" : "Connecting",
      };
    if (busy) return { dot: "primary" as const, label: "Thinking" };
    // A turn reattached after a reopen is still running even though this tab
    // never sent it, so `busy` is false. Lags a completing turn by one poll.
    if (activeTaskId) return { dot: "primary" as const, label: "Working" };
    return { dot: "ready" as const, label: "Ready" };
  }, [client, busy, activeTaskId, bootError, sessionLost, historyLoading]);

  const contextId = client?.contextId ?? null;
  const activeContextId = contextIdProp ?? null;

  // Horizon could assume an id in the URL always has history, because its
  // sessions are server-side. AQuA's conversations are A2A contexts, so an id
  // can be new or stale and resolve to nothing -- keying only on `contextIdProp`
  // left those staring at a blank pane with no composer prompt. Wait for the
  // load to settle, then treat "still no messages" as a new chat.
  // When `initialTurnBooting` (`?q=...` from "Diagnose in chat") is active,
  // render the conversation layout and optimistic prompt bubble from Frame 0
  // using the same message IDs `send()` will adopt once `createLhaClient`
  // resolves.
  const showWelcome =
    messages.length === 0 && !historyLoading && !initialTurnBooting;
  const displayMessages = useMemo<ChatMessage[]>(() => {
    if (!initialTurnBooting || !trimmedInitialMessage) return messages;
    const ids = initialTurnIdsRef.current ?? {
      user: "initial-user-preview",
      assistant: "initial-assistant-preview",
      createdAt: Date.now(),
    };
    return [
      {
        id: ids.user,
        role: "user",
        segments: [{ kind: "text", text: trimmedInitialMessage }],
        createdAt: ids.createdAt,
      },
      {
        id: ids.assistant,
        role: "assistant",
        segments: [],
        pending: true,
        createdAt: ids.createdAt + 1,
      },
    ];
  }, [messages, initialTurnBooting, trimmedInitialMessage]);

  const activeSession = useMemo(
    () => sessions?.find((s) => s.id === activeContextId) ?? null,
    [sessions, activeContextId],
  );

  const handleExport = useCallback(
    (format: "markdown" | "json") => {
      if (displayMessages.length === 0) return;
      downloadConversation(
        displayMessages,
        activeSession?.title ?? "chat",
        format,
      );
      notify.success(
        `Exported as ${format === "markdown" ? "Markdown" : "JSON"}`,
      );
    },
    [displayMessages, activeSession],
  );

  const headerControls = useChatTitleControls({
    sessionId: activeSession?.id ?? "",
    title: activeSession?.title ?? "",
    onAfterRename: refreshSessions,
    onAfterDelete: () => {
      refreshSessions();
      navigate({ to: "/c", search: {} });
    },
  });

  // The panel's Export action. A short text keeps the bubble readable; the
  // DataPart carries the instruction, so none of it lands in the transcript.
  const selectionChip = pendingSelection
    ? { label: anchorLabel(pendingSelection.anchor.snippet) }
    : null;

  // No right-pane header any more: Activity was the only panel AQuA had a use
  // for and tool calls already render inline in the transcript, so it was the
  // same information twice. Appearance and feedback moved up to the tab bar,
  // which is global. What is left is the artifact viewer.
  const rightPane = (
    <div className="flex h-full min-h-0 flex-col">
      <ArtifactViewer />
    </div>
  );

  return (
    <NowProvider>
      <ImageLightboxProvider>
        <ViewerProvider
          resetKey={contextId}
          onOpen={handleArtifactOpen}
          onSelectionChange={setPendingSelection}
        >
          {/* Hoisted above the layout so the artifact viewer can disable editing
        mid-turn, not just the message list's resend/edit buttons. */}
          <BusyProvider value={turnActive}>
            <div className="flex h-full flex-row overflow-hidden bg-background">
              {/* `SidebarRail` owns the rail on every route, so its width and
                  collapsed state are one saved layout rather than this
                  route's. The group below is nested inside it. */}
              <SidebarRail
                showRail={isDesktop}
                activeContextId={activeContextId}
                sessions={sessions}
                sessionsLoading={sessionsLoading}
                refreshSessions={refreshSessions}
                onNewChat={onNewChat}
                onPrefetch={handlePrefetchChat}
              >
                <ResizablePanelGroup
                  direction="horizontal"
                  // Bumped from "lha.layout": the artifact pane's default went
                  // from a quarter of the window to collapsed, and a saved
                  // layout would have kept showing "No file open" to everyone
                  // who had already loaded the old build.
                  autoSaveId="aqua.layout.v2"
                  className="flex-1"
                >
                  <ResizablePanel
                    id="center"
                    order={2}
                    minSize={30}
                    className="flex min-h-0 min-w-0 flex-col"
                  >
                    {/* min-h-0 all the way down so the message list (not the column) is
              the thing that scrolls, keeping the composer anchored. */}
                    <div className="flex min-h-0 min-w-0 flex-1 flex-col motion-safe:animate-fade-in-up">
                      <header className="group/header flex h-12 shrink-0 items-center justify-between gap-2 border-b px-4">
                        <div className="flex min-w-0 flex-1 items-center gap-2">
                          <Sheet
                            open={sidebarOpen}
                            onOpenChange={setSidebarOpen}
                          >
                            <SheetTrigger asChild>
                              <Button
                                variant="ghost"
                                size="icon"
                                className="h-10 w-10 md:hidden"
                              >
                                <PanelLeft className="h-4 w-4" />
                                <span className="sr-only">Chat history</span>
                              </Button>
                            </SheetTrigger>
                            <SheetContent side="left" className="w-72 p-0">
                              <SheetHeader className="sr-only">
                                <SheetTitle>Chats</SheetTitle>
                                <SheetDescription>
                                  Chat history
                                </SheetDescription>
                              </SheetHeader>
                              {sidebarOpen && (
                                <ChatListSidebar
                                  activeContextId={activeContextId}
                                  sessions={sessions}
                                  sessionsLoading={sessionsLoading}
                                  refreshSessions={refreshSessions}
                                  onNewChat={onNewChat}
                                  onNavigate={() => setSidebarOpen(false)}
                                  onPrefetch={handlePrefetchChat}
                                  reserveHeaderRight
                                />
                              )}
                            </SheetContent>
                          </Sheet>
                          {/* No wordmark here: the app bar above already shows
                            it, and the two sat a couple of centimetres apart.
                            The conversation title is what this header is for. */}
                          {activeSession && (
                            <>
                              <span
                                aria-hidden
                                className="hidden shrink-0 text-muted-foreground/40"
                              >
                                ·
                              </span>
                              {headerControls.editing ? (
                                <ChatTitleInput
                                  controls={headerControls}
                                  className={cn(
                                    textStyle.body,
                                    "h-7 max-w-[40ch] px-1.5 py-0",
                                  )}
                                />
                              ) : (
                                <span
                                  className={cn(
                                    textStyle.body,
                                    "min-w-0 truncate",
                                  )}
                                  title={activeSession.title}
                                >
                                  {activeSession.title}
                                </span>
                              )}
                              <div className="pointer-events-none flex shrink-0 items-center gap-0.5 opacity-0 transition-opacity group-hover/header:pointer-events-auto group-hover/header:opacity-100 focus-within:pointer-events-auto focus-within:opacity-100 max-md:pointer-events-auto max-md:opacity-100">
                                <ChatActionButtons
                                  title={activeSession.title}
                                  controls={headerControls}
                                />
                              </div>
                            </>
                          )}
                        </div>
                        <div className="flex shrink-0 items-center gap-2">
                          <span
                            role="status"
                            aria-live="polite"
                            aria-label={`agent ${status.label}`}
                            className={cn(
                              textStyle.meta,
                              "flex items-center gap-1.5",
                            )}
                          >
                            <span
                              aria-hidden="true"
                              className={cn(
                                "h-1.5 w-1.5 rounded-full",
                                status.dot === "destructive" &&
                                  "bg-destructive",
                                status.dot === "muted" &&
                                  "bg-muted-foreground/60 motion-safe:animate-pulse",
                                status.dot === "primary" &&
                                  "bg-primary motion-safe:animate-pulse",
                                status.dot === "ready" && "bg-emerald-500",
                              )}
                            />
                            {status.label}
                          </span>
                          {isDesktop && (
                            <Button
                              variant="ghost"
                              size="icon"
                              className="h-10 w-10"
                              onClick={toggleRightPanel}
                              aria-label={
                                rightCollapsed ? "Show panel" : "Hide panel"
                              }
                              title={
                                rightCollapsed ? "Show panel" : "Hide panel"
                              }
                            >
                              {rightCollapsed ? (
                                <PanelRightOpen className="h-4 w-4" />
                              ) : (
                                <PanelRightClose className="h-4 w-4" />
                              )}
                            </Button>
                          )}
                          <Sheet
                            open={!isDesktop && panelsOpen}
                            onOpenChange={setPanelsOpen}
                          >
                            <SheetTrigger asChild>
                              <Button
                                variant="ghost"
                                size="icon"
                                className="h-10 w-10 md:hidden"
                              >
                                <PanelRight className="h-4 w-4" />
                                <span className="sr-only">Chat panels</span>
                              </Button>
                            </SheetTrigger>
                            <SheetContent
                              side="right"
                              className="flex max-w-sm flex-col p-0"
                            >
                              <SheetHeader className="sr-only">
                                <SheetTitle>Chat</SheetTitle>
                                <SheetDescription>
                                  Artifact viewer
                                </SheetDescription>
                              </SheetHeader>
                              {panelsOpen && rightPane}
                            </SheetContent>
                          </Sheet>
                        </div>
                      </header>

                      <ChatDeleteError
                        error={headerControls.deleteError}
                        className="mx-4 mt-1"
                      />

                      {sessionLost && <SessionLostBanner />}
                      <GuardrailBanner
                        contextId={contextId}
                        bootError={bootError}
                      />

                      <main
                        data-testid="transcript"
                        onDragEnter={onDragEnter}
                        onDragLeave={onDragLeave}
                        onDragOver={onDragOver}
                        onDrop={onDrop}
                        className="relative flex min-h-0 flex-1 flex-col"
                      >
                        {dragOver && (
                          <div
                            className={cn(
                              textStyle.itemTitle,
                              "pointer-events-none absolute inset-0 z-20 flex items-center justify-center bg-primary/10 text-primary ring-2 ring-inset ring-primary",
                            )}
                          >
                            Drop files to attach
                          </div>
                        )}
                        {showWelcome ? (
                          <div className="flex min-h-0 flex-1 flex-col overflow-y-auto px-4">
                            <div className="flex flex-1 flex-col items-center justify-start pt-6 md:pt-24">
                              <div className="w-full max-w-2xl">
                                <div className="text-center">
                                  <h1 className={textStyle.pageTitle}>
                                    <Wordmark label="AQuA" />
                                  </h1>
                                  <p
                                    className={cn(
                                      textStyle.description,
                                      "mx-auto mt-4 max-w-xl",
                                    )}
                                  >
                                    Ask about the insights AQuA found in the
                                    observed agent.
                                  </p>
                                </div>
                                <div className="mt-6">
                                  <InputBox
                                    onSend={handleSend}
                                    onStop={handleStop}
                                    value={draft}
                                    onValueChange={onDraftChange}
                                    queued={queue.map((q) => q.text)}
                                    onRemoveQueued={removeQueued}
                                    disabled={!client}
                                    busy={turnActive}
                                    attachments={attachments}
                                    onAddFiles={addFiles}
                                    onRemoveAttachment={removeAttachment}
                                    attachmentError={attachmentError}
                                    hasPendingPrefix={Boolean(pendingSelection)}
                                    selectionChip={selectionChip}
                                    onClearSelection={() =>
                                      setPendingSelection(null)
                                    }
                                  />
                                </div>
                                <PromptChips
                                  onSend={handleSend}
                                  disabled={!client || busy}
                                />
                              </div>
                            </div>
                          </div>
                        ) : (
                          <>
                            {findOpen && (
                              <FindBar
                                messages={displayMessages}
                                onClose={handleFindClose}
                                onActiveMatchChange={handleFindActiveMatch}
                                focusSignal={findFocusSignal}
                              />
                            )}
                            <ConfirmationProvider sender={confirmationSender}>
                              <MessageList
                                messages={displayMessages}
                                contextId={contextId}
                                onResend={resend}
                                onEdit={editAndResend}
                                highlightId={findHighlightId}
                              />
                            </ConfirmationProvider>
                            <DormantActionsGate
                              lastUpdated={activeSession?.lastUpdated ?? null}
                              show={
                                messages.length > 0 &&
                                !busy &&
                                !dormantActionsDismissed
                              }
                              disabled={!client || busy}
                              onAction={(prompt) => {
                                handleSend(prompt);
                                setDormantActionsDismissed(true);
                              }}
                            />
                            <InputBox
                              onSend={handleSend}
                              onStop={handleStop}
                              value={draft}
                              onValueChange={onDraftChange}
                              queued={queue.map((q) => q.text)}
                              onRemoveQueued={removeQueued}
                              disabled={!client}
                              busy={turnActive}
                              attachments={attachments}
                              onAddFiles={addFiles}
                              onRemoveAttachment={removeAttachment}
                              attachmentError={attachmentError}
                              hasPendingPrefix={Boolean(pendingSelection)}
                              selectionChip={selectionChip}
                              onClearSelection={() => setPendingSelection(null)}
                            />
                          </>
                        )}
                      </main>
                    </div>
                  </ResizablePanel>

                  {isDesktop && (
                    <>
                      <ResizableHandle withHandle />
                      <ResizablePanel
                        id="right"
                        order={3}
                        ref={rightPanelRef}
                        // Collapsed until there is an artifact: AQuA's agent is
                        // read-only and rarely produces files, so the default
                        // gave a quarter of the window to "No file open".
                        // `handleArtifactOpen` expands it when one arrives.
                        defaultSize={0}
                        minSize={18}
                        maxSize={50}
                        collapsible
                        collapsedSize={0}
                        onCollapse={() => setRightCollapsed(true)}
                        onExpand={() => setRightCollapsed(false)}
                        className="flex flex-col border-l bg-card/30"
                      >
                        {rightPane}
                      </ResizablePanel>
                    </>
                  )}
                </ResizablePanelGroup>
              </SidebarRail>
              <CommandPalette
                open={paletteOpen}
                onOpenChange={setPaletteOpen}
                sessions={sessions}
                activeContextId={contextId}
                onNewChat={onNewChat}
                canExport={displayMessages.length > 0}
                onExport={handleExport}
              />
            </div>
          </BusyProvider>
        </ViewerProvider>
      </ImageLightboxProvider>
    </NowProvider>
  );
}

function hasFileDrag(e: DragEvent<HTMLElement>): boolean {
  const types = e.dataTransfer?.types;
  if (!types) return false;
  for (let i = 0; i < types.length; i++) {
    if (types[i] === "Files") return true;
  }
  return false;
}

interface PromptChip {
  label: string;
  Icon: LucideIcon;
}

// Every starter has to be something the chat agent's tools can actually do.
// "Run a full investigation" is first because it is the one action that
// produces new data rather than reading what is already there.
const PROMPT_CHIPS: PromptChip[] = [
  { label: "Run a full investigation", Icon: Play },
  { label: "What are the worst insights right now?", Icon: LineChart },
  { label: "Run a new custom investigation", Icon: ListFilter },
  { label: "Are any of these the same underlying bug?", Icon: Brain },
  { label: "Which insights have no verified diagnosis yet?", Icon: Newspaper },
  { label: "What changed since the last investigation?", Icon: CalendarClock },
];

function PromptChips({
  onSend,
  disabled,
}: {
  onSend: (t: string) => void;
  disabled?: boolean;
}) {
  return (
    <div className="mt-6 flex flex-col items-center gap-3">
      <span className={textStyle.label}>Try a starter</span>
      <div className="flex flex-wrap items-center justify-center gap-2">
        {PROMPT_CHIPS.map(({ label, Icon }) => (
          <button
            key={label}
            type="button"
            disabled={disabled}
            onClick={() => onSend(label)}
            className={cn(
              textStyle.body,
              "group/chip inline-flex items-center gap-2 rounded-full border border-border bg-card px-4 py-2.5 shadow-sm transition-all hover:-translate-y-0.5 hover:border-primary/40 hover:bg-primary/5 hover:shadow focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 disabled:cursor-not-allowed disabled:opacity-50 disabled:hover:translate-y-0 disabled:hover:shadow-sm",
            )}
          >
            <Icon className="h-3.5 w-3.5 text-primary/70 transition-colors group-hover/chip:text-primary" />
            {label}
          </button>
        ))}
      </div>
    </div>
  );
}
