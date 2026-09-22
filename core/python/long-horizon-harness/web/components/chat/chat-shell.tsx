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
import { useLhaState } from "@/lib/horizon-state";
import { setChatWindow, useLhaSessions } from "@/lib/horizon-sessions";
import { takePendingWindow } from "@/lib/pending-window";
import { useSandboxStatus, warmSandbox } from "@/lib/horizon-sandbox-status";
import { MessageList } from "./message-list";
import { InputBox, formatBytes, type InputAttachment } from "./input-box";
import { SidePanels } from "./side-panels";
import { ChatListSidebar } from "./chat-list-sidebar";
import {
  ResizablePanelGroup,
  ResizablePanel,
  ResizableHandle,
} from "@/components/ui/resizable";
import { ArtifactViewer } from "./artifact-viewer/artifact-viewer";
import { AutoOpenNewFile } from "./artifact-viewer/auto-open-new-file";
import {
  ViewerProvider,
  type PendingSelection,
} from "./artifact-viewer/viewer-context";
import { anchorLabel } from "@/lib/selection-anchor";
import type { Part } from "@a2a-js/sdk";
import { dataPartDict, makeDataPart } from "@/lib/a2a-part";
import {
  FILEREF_DATA_KIND,
  SELECTION_DATA_KIND,
  isFileRefPayload,
  isSelectionPayload,
  type FileRef,
  type FileRefPayload,
  type SelectionPayload,
} from "./workspace-href";
import { useMediaQuery } from "@/lib/use-media-query";
import type { ImperativePanelHandle } from "react-resizable-panels";
import type { UploadResult } from "./sidebar-dropzone";
import type { RecentUpload } from "./sidebar-upload-list";
import type { OptimisticRow } from "./sidebar-workspace-tree";
import {
  ChatActionButtons,
  ChatDeleteError,
  ChatTitleInput,
  useChatTitleControls,
} from "./chat-title-controls";
import { GuardrailBanner } from "@/components/panels/guardrail-banner";
import { SandboxWarmupBanner } from "./sandbox-warmup-banner";
import { SessionLostBanner } from "./session-lost-banner";
import { isTransportError } from "@/lib/is-transport-error";
import { Button } from "@/components/ui/button";
import {
  Sheet,
  SheetContent,
  SheetHeader,
  SheetTitle,
  SheetTrigger,
} from "@/components/ui/sheet";
import {
  Brain,
  CalendarClock,
  Files,
  LineChart,
  List,
  Newspaper,
  PanelLeft,
  PanelRight,
  PanelRightClose,
  PanelRightOpen,
  Sparkles,
  type LucideIcon,
} from "lucide-react";

import { cn } from "@/lib/utils";
import { useLhaTasks } from "@/lib/horizon-tasks";
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
import { AuthCard } from "./auth-card";
import { Wordmark } from "@/components/brand/wordmark";
import { readErrorDetail } from "@/lib/read-error-detail";
import { CommandPalette } from "./command-palette";
import { FindBar } from "./find-bar";
import { ImageLightboxProvider } from "./image-lightbox";
import { focusComposer } from "@/lib/composer-focus";
import { AppearancePopover } from "@/components/chat/appearance-popover";
import { FeedbackPopover } from "@/components/chat/feedback-popover";
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
}

const MAX_FILE_BYTES = 20 * 1024 * 1024;
const IMAGE_PREFIX = "image/";
// Sticky context id: lets a closed-then-reopened tab land back on the last
// conversation so useLhaTasks can spot an in-flight task and useTaskResubscribe
// auto-reattaches to a server-side run that survived the disconnect.
const LAST_CONTEXT_STORAGE_KEY = "lha:lastContextId";

export function ChatShell({ contextId: contextIdProp }: ChatShellProps = {}) {
  const [client, setClient] = useState<HorizonClient | null>(null);
  // The contextId the current client was booted for. Lets the boot effect skip
  // a rebuild when the first-send URL lock flips contextIdProp from undefined to
  // the id the live client already owns (so the in-flight stream survives).
  const bootedContextIdRef = useRef<string | null>(null);
  const [bootError, setBootError] = useState<string | null>(null);
  const [sessionLost, setSessionLost] = useState(false);
  const [warmError, setWarmError] = useState<string | null>(null);
  const [panelsOpen, setPanelsOpen] = useState(false);
  const [sidebarOpen, setSidebarOpen] = useState(false);
  const [rightMode, setRightMode] = useState<"overview" | "viewer">("overview");
  const [rightCollapsed, setRightCollapsed] = useState(false);
  const rightPanelRef = useRef<ImperativePanelHandle>(null);
  const isDesktop = useMediaQuery("(min-width: 768px)");
  const handleArtifactOpen = useCallback(() => {
    setRightMode("viewer");
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
  // Workspace upload state. Pending names are flushed into the next outgoing
  // message as a `[Uploaded to /workspace: ...]` prefix so the agent sees the
  // new files on its next turn.
  const [pendingUploadNames, setPendingUploadNames] = useState<string[]>([]);
  // Text highlighted in the artifact panel, prepended to the next message as
  // an `[Editing …]` block so the agent can resolve it with `patch`. Owned
  // here because this component renders ViewerProvider and so can't read its
  // context; the viewer reports upward via onSelectionChange.
  const [pendingSelection, setPendingSelection] =
    useState<PendingSelection | null>(null);
  // Set when the panel's Export starts a turn, so its result claims the panel
  // rather than waiting for an empty slot. Cleared once that turn is over.
  // Workspace files dragged onto the composer. Paths only — the bytes are
  // already on disk, so an upload would duplicate them.
  const [fileRefs, setFileRefs] = useState<FileRef[]>([]);
  const addFileRef = useCallback((ref: FileRef) => {
    setFileRefs((prev) =>
      prev.some((r) => r.path === ref.path) ? prev : [...prev, ref],
    );
  }, []);
  const removeFileRef = useCallback((path: string) => {
    setFileRefs((prev) => prev.filter((r) => r.path !== path));
  }, []);
  const [recentUploads, setRecentUploads] = useState<RecentUpload[]>([]);
  const [workspaceVersion, setWorkspaceVersion] = useState(0);
  const [optimisticRows, setOptimisticRows] = useState<OptimisticRow[]>([]);
  // Debounce tree refetches so a 10-file drop fires one fetch, not ten.
  const treeBumpTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  // dragenter/dragleave fire on every nested element. Counting depth avoids
  // the overlay flickering when the cursor crosses child boundaries.
  const dragDepthRef = useRef(0);

  const navigate = useNavigate();
  const {
    data: sessions,
    isLoading: sessionsLoading,
    refresh: refreshSessions,
  } = useLhaSessions();
  const { data: sandboxStatus } = useSandboxStatus();

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

  const bumpWorkspaceVersion = useCallback(() => {
    setWorkspaceVersion((v) => v + 1);
  }, []);

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
  const recordLiveTaskId = useCallback((id: string) => {
    liveTaskIdsRef.current.add(id);
  }, []);
  useEffect(() => {
    liveTaskIdsRef.current = new Set();
  }, [contextIdProp]);

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
      if (touchedFs) bumpWorkspaceVersion();
      // Pull the now-complete task into history.
      void refreshTasks();
    },
    onSessionLost: handleSessionLost,
    onLiveTaskId: recordLiveTaskId,
    onFirstSend: lockContextUrl,
  });

  // Must stay after useChatStream: its busyRef-reset effect has to run before
  // this hook's flush effect, or the flush's send() would hit the busy guard.
  // `busy` only covers a turn this tab sent. A turn reattached after a reopen
  // is equally in flight, and sending into it starts a second concurrent turn
  // against the same session. Composer-facing state must use this instead.
  const turnActive = busy || activeTaskId !== null;

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
  useEffect(() => {
    if (contextIdProp) return;
    if (typeof window === "undefined") return;
    const stored = window.localStorage.getItem(LAST_CONTEXT_STORAGE_KEY);
    if (!stored) return;
    navigate({ to: "/c", search: { id: stored }, replace: true });
  }, [contextIdProp, navigate]);

  // Boot or rebuild the A2A client whenever the URL contextId changes.
  // No prop → generate a fresh contextId for a new chat; on first send we lock
  // ?id={contextId} into the URL so it becomes bookmarkable.
  useEffect(() => {
    const desired = contextIdProp ?? null;
    // First send locks the URL to the contextId the live client already owns
    // (undefined -> that id). Skip the rebuild so the in-flight stream and the
    // pending bubbles survive the URL change.
    if (desired && bootedContextIdRef.current === desired) return;

    let cancelled = false;
    const bootAbort = new AbortController();
    setClient(null);
    setBootError(null);
    setSessionLost(false);
    setMessages([]);
    setDraft(loadDraft(contextIdProp));
    clearQueue();

    const contextId = contextIdProp ?? crypto.randomUUID();
    // Front-load sandbox provisioning so the user isn't waiting 30-60s
    // on their first message. The status endpoint only tracks warms it
    // received, so capture the POST error locally too.
    setWarmError(null);
    void warmSandbox().catch((err) => {
      if (cancelled) return;
      setWarmError(err instanceof Error ? err.message : String(err));
      notify.error("Sandbox warm-up failed", {
        description: err instanceof Error ? err.message : String(err),
      });
    });
    createLhaClient({ contextId, signal: bootAbort.signal })
      .then((c) => {
        if (cancelled) return;
        // Stamp only once the client exists. Stamping before the await let an
        // aborted boot (StrictMode's mount->cleanup->mount) claim the id, so the
        // re-run returned early and no client was ever built.
        bootedContextIdRef.current = contextId;
        setClient(c);
        if (typeof window !== "undefined") {
          window.localStorage.setItem(LAST_CONTEXT_STORAGE_KEY, c.contextId);
        }
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
    if (typeof window !== "undefined") {
      window.localStorage.removeItem(LAST_CONTEXT_STORAGE_KEY);
    }
    if (contextIdProp) {
      navigate({ to: "/" });
    } else {
      setResetKey((k) => k + 1);
      setAttachments([]);
      setAttachmentError(null);
    }
  }, [contextIdProp, navigate]);

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
  useEffect(() => {
    if (!historyMessages || busy) return;
    setMessages((prev) => mergeHistoryWithLive(prev, historyMessages));
    // activeTaskId stays in deps as a refetch trigger (it changes when a turn
    // settles); the merge itself no longer reads it.
    // eslint-disable-next-line react-hooks/exhaustive-deps
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

  const handleSend = useCallback(
    (text: string) => {
      // Built before the busy check so a message queued mid-turn keeps its
      // selection and file references; flushing text alone would send the
      // instruction without the span it refers to.
      const queuedParts: Part[] = [];
      if (fileRefs.length > 0) {
        queuedParts.push(
          makeDataPart({
            lha_kind: FILEREF_DATA_KIND,
            paths: fileRefs.map((r) => r.path),
          } satisfies FileRefPayload),
        );
      }
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
      if (turnActive) {
        enqueue(text, queuedParts);
        if (queuedParts.length > 0) {
          setFileRefs([]);
          setPendingSelection(null);
        }
        return;
      }
      const sending = attachments;
      setAttachments([]);
      setAttachmentError(null);
      let outgoing = text;
      if (pendingUploadNames.length > 0) {
        const prefix = `[Uploaded to /workspace/uploads: ${pendingUploadNames.join(", ")}]`;
        outgoing = text.trim() ? `${prefix}\n\n${text}` : prefix;
        setPendingUploadNames([]);
      }
      if (fileRefs.length > 0) setFileRefs([]);
      if (pendingSelection) setPendingSelection(null);
      const outgoingParts = queuedParts;
      // A project-chat seeds its workspace_window before the first turn so the
      // agent's file ops default into /workspace/<project>. Best-effort: a seed
      // failure must never swallow the user's message.
      const cid = client?.contextId;
      const dirs =
        cid && messages.length === 0 ? takePendingWindow(cid) : undefined;
      if (cid && dirs && dirs.length > 0) {
        void setChatWindow(cid, dirs).finally(() =>
          send(outgoing, {
            attachments: sending,
            extraParts: outgoingParts.length > 0 ? outgoingParts : undefined,
          }),
        );
      } else {
        void send(outgoing, {
          attachments: sending,
          extraParts: outgoingParts.length > 0 ? outgoingParts : undefined,
        });
      }
    },
    [
      turnActive,
      enqueue,
      attachments,
      pendingUploadNames,
      pendingSelection,
      fileRefs,
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
    const restoredRefs = queuedPayloads
      .filter(isFileRefPayload)
      .flatMap((d) => d.paths)
      .map((path) => ({ path, name: path.split("/").pop() || path }));
    if (restoredRefs.length > 0) {
      setFileRefs((prev) => {
        const seen = new Set(prev.map((r) => r.path));
        return [...prev, ...restoredRefs.filter((r) => !seen.has(r.path))];
      });
    }
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

  const handleUploadComplete = useCallback((result: UploadResult) => {
    setPendingUploadNames((prev) =>
      prev.includes(result.name) ? prev : [...prev, result.name],
    );
    setRecentUploads((prev) => [
      { name: result.name, size: result.size, uploadedAt: Date.now() },
      ...prev.filter((r) => r.name !== result.name),
    ]);
    setOptimisticRows((prev) => [
      ...prev.filter((r) => r.name !== result.name),
      { name: result.name, size: result.size, mtime: Date.now() },
    ]);
  }, []);

  const handleWorkspaceChanged = useCallback(() => {
    if (treeBumpTimerRef.current) clearTimeout(treeBumpTimerRef.current);
    treeBumpTimerRef.current = setTimeout(() => {
      treeBumpTimerRef.current = null;
      bumpWorkspaceVersion();
    }, 300);
  }, [bumpWorkspaceVersion]);

  const handleReconciled = useCallback(() => {
    // Server listing has caught up — drop optimistic rows so we don't render
    // them on top of canonical ones (the tree dedups by name, but clearing
    // here also keeps the buffer from growing without bound).
    setOptimisticRows([]);
  }, []);

  const handleClearRecent = useCallback(() => {
    setRecentUploads([]);
  }, []);

  const handleDeleteWorkspaceFile = useCallback(
    async (path: string, opts?: { recursive?: boolean }) => {
      const params = new URLSearchParams({ path });
      if (opts?.recursive) params.set("recursive", "true");
      const resp = await fetch(`/lha/workspace/file?${params}`, {
        method: "DELETE",
      });
      if (!resp.ok) {
        const detail = await readErrorDetail(resp).catch(() => "");
        throw new Error(
          `${resp.status}${detail ? `: ${detail.slice(0, 200)}` : ""}`,
        );
      }
      const name = path.split("/").pop();
      if (name) {
        setRecentUploads((prev) => prev.filter((r) => r.name !== name));
        setOptimisticRows((prev) => prev.filter((r) => r.name !== name));
        setPendingUploadNames((prev) => prev.filter((n) => n !== name));
      }
      bumpWorkspaceVersion();
    },
    [bumpWorkspaceVersion],
  );

  useEffect(() => {
    return () => {
      if (treeBumpTimerRef.current) clearTimeout(treeBumpTimerRef.current);
    };
  }, []);

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

  const status = useMemo(() => {
    if (bootError || sessionLost)
      return { dot: "destructive" as const, label: "disconnected" };
    if (!client || historyLoading)
      return {
        dot: "muted" as const,
        label: historyLoading ? "loading" : "connecting",
      };
    if (busy) return { dot: "primary" as const, label: "thinking" };
    // A turn reattached after a reopen is still running even though this tab
    // never sent it, so `busy` is false. Lags a completing turn by one poll.
    if (activeTaskId) return { dot: "primary" as const, label: "working" };
    return { dot: "ready" as const, label: "ready" };
  }, [client, busy, activeTaskId, bootError, sessionLost, historyLoading]);

  const contextId = client?.contextId ?? null;
  const activeContextId = contextIdProp ?? null;

  // Empty messages on `/c?id=…` means history is still loading — not a new chat.
  const showWelcome = messages.length === 0 && !contextIdProp;

  const activeSession = useMemo(
    () => sessions?.find((s) => s.id === activeContextId) ?? null,
    [sessions, activeContextId],
  );

  const handleExport = useCallback(
    (format: "markdown" | "json") => {
      if (messages.length === 0) return;
      downloadConversation(
        messages,
        activeSession?.title ?? "conversation",
        format,
      );
      notify.success(
        `Exported as ${format === "markdown" ? "Markdown" : "JSON"}`,
      );
    },
    [messages, activeSession],
  );

  const headerControls = useChatTitleControls({
    sessionId: activeSession?.id ?? "",
    title: activeSession?.title ?? "",
    onAfterRename: refreshSessions,
    onAfterDelete: () => {
      refreshSessions();
      navigate({ to: "/" });
    },
  });

  // The panel's Export action. A short text keeps the bubble readable; the
  // DataPart carries the instruction, so none of it lands in the transcript.
  const selectionChip = pendingSelection
    ? { label: anchorLabel(pendingSelection.anchor.snippet) }
    : null;

  const rightPaneHeader = (
    // Clears the Sheet's own absolute close button on mobile.
    <div className="flex h-12 shrink-0 items-center justify-between gap-2 border-b px-3 max-md:pr-14">
      <div className="flex items-center gap-0.5 rounded-md border border-border/60 p-0.5">
        <button
          type="button"
          aria-label="Overview"
          aria-pressed={rightMode === "overview"}
          onClick={() => setRightMode("overview")}
          className={cn(
            "rounded p-1 transition-colors",
            rightMode === "overview"
              ? "bg-muted text-foreground"
              : "text-muted-foreground hover:text-foreground",
          )}
        >
          <List className="h-3.5 w-3.5" />
        </button>
        <button
          type="button"
          aria-label="Files"
          aria-pressed={rightMode === "viewer"}
          onClick={() => setRightMode("viewer")}
          className={cn(
            "rounded p-1 transition-colors",
            rightMode === "viewer"
              ? "bg-muted text-foreground"
              : "text-muted-foreground hover:text-foreground",
          )}
        >
          <Files className="h-3.5 w-3.5" />
        </button>
      </div>
      <div className="flex items-center gap-1">
        <AppearancePopover />
        <FeedbackPopover sessionId={contextId ?? undefined} />
      </div>
    </div>
  );
  const rightPane = (
    <div className="flex h-full min-h-0 flex-col">
      {rightPaneHeader}
      <div className="min-h-0 flex-1">
        {rightMode === "viewer" ? (
          <ArtifactViewer />
        ) : (
          <div className="h-full overflow-y-auto p-3">
            <SidePanels contextId={contextId} />
          </div>
        )}
      </div>
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
            <AutoOpenNewFile />
            <div className="flex h-dvh flex-row overflow-hidden bg-background">
              <ResizablePanelGroup
                direction="horizontal"
                autoSaveId="lha.layout"
                className="flex-1"
              >
                {isDesktop && (
                  <>
                    <ResizablePanel
                      id="left"
                      order={1}
                      defaultSize={18}
                      minSize={14}
                      maxSize={30}
                      className="flex flex-col border-r bg-card/30"
                    >
                      <ChatListSidebar
                        activeContextId={activeContextId}
                        sessions={sessions}
                        sessionsLoading={sessionsLoading}
                        refreshSessions={refreshSessions}
                        onNewChat={onNewChat}
                        onPrefetch={handlePrefetchChat}
                        recentUploads={recentUploads}
                        optimisticRows={optimisticRows}
                        workspaceVersion={workspaceVersion}
                        onUploadComplete={handleUploadComplete}
                        onWorkspaceChanged={handleWorkspaceChanged}
                        onReconciled={handleReconciled}
                        onClearRecent={handleClearRecent}
                        onDeleteWorkspaceFile={handleDeleteWorkspaceFile}
                      />
                    </ResizablePanel>
                    <ResizableHandle withHandle />
                  </>
                )}

                <ResizablePanel
                  id="center"
                  order={2}
                  minSize={30}
                  className="flex min-h-0 min-w-0 flex-col"
                >
                  {/* min-h-0 all the way down so the message list (not the column) is
              the thing that scrolls, keeping the composer anchored. */}
                  <div className="flex min-h-0 min-w-0 flex-1 flex-col">
                    <header className="group/header flex h-12 shrink-0 items-center justify-between gap-2 border-b px-4">
                      <div className="flex min-w-0 flex-1 items-center gap-2">
                        <Sheet open={sidebarOpen} onOpenChange={setSidebarOpen}>
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
                                recentUploads={recentUploads}
                                optimisticRows={optimisticRows}
                                workspaceVersion={workspaceVersion}
                                onUploadComplete={handleUploadComplete}
                                onWorkspaceChanged={handleWorkspaceChanged}
                                onReconciled={handleReconciled}
                                onClearRecent={handleClearRecent}
                                onDeleteWorkspaceFile={
                                  handleDeleteWorkspaceFile
                                }
                              />
                            )}
                          </SheetContent>
                        </Sheet>
                        <Wordmark
                          label="Long Horizon Harness"
                          className="min-w-0 truncate text-base font-semibold tracking-tight"
                        />
                        {activeSession && (
                          <>
                            <span
                              aria-hidden
                              className="shrink-0 text-muted-foreground/40"
                            >
                              ·
                            </span>
                            {headerControls.editing ? (
                              <ChatTitleInput
                                controls={headerControls}
                                className="h-7 max-w-[40ch] px-1.5 py-0 text-sm"
                              />
                            ) : (
                              <span
                                className="min-w-0 truncate text-sm text-foreground/90"
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
                        {sandboxStatus?.user_id && (
                          <span
                            title={`signed in as ${sandboxStatus.user_id}`}
                            className="hidden max-w-[24ch] truncate rounded-full border border-border/40 bg-muted/40 px-2 py-0.5 font-mono text-[10px] text-muted-foreground sm:inline-block"
                          >
                            {sandboxStatus.user_id}
                          </span>
                        )}
                        <span
                          role="status"
                          aria-live="polite"
                          aria-label={`agent ${status.label}`}
                          className="flex items-center gap-1.5 text-[11px] text-muted-foreground"
                        >
                          <span
                            aria-hidden="true"
                            className={cn(
                              "h-1.5 w-1.5 rounded-full",
                              status.dot === "destructive" && "bg-destructive",
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
                            title={rightCollapsed ? "Show panel" : "Hide panel"}
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
                              <span className="sr-only">Session panels</span>
                            </Button>
                          </SheetTrigger>
                          <SheetContent
                            side="right"
                            className="flex max-w-sm flex-col p-0"
                          >
                            <SheetHeader className="sr-only">
                              <SheetTitle>Session</SheetTitle>
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
                      onDragEnter={onDragEnter}
                      onDragLeave={onDragLeave}
                      onDragOver={onDragOver}
                      onDrop={onDrop}
                      className="relative flex min-h-0 flex-1 flex-col"
                    >
                      {dragOver && (
                        <div className="pointer-events-none absolute inset-0 z-20 flex items-center justify-center bg-primary/10 text-sm font-medium text-primary ring-2 ring-inset ring-primary">
                          Drop files to attach
                        </div>
                      )}
                      {showWelcome ? (
                        <div className="flex min-h-0 flex-1 flex-col overflow-y-auto px-4">
                          <div className="flex flex-1 flex-col items-center justify-start pt-6 md:pt-24">
                            <div className="w-full max-w-2xl">
                              <div className="text-center">
                                {sandboxStatus?.user_id && (
                                  <p className="mb-3 text-lg text-muted-foreground md:text-xl">
                                    Hi,{" "}
                                    <span className="font-medium text-foreground">
                                      {sandboxStatus.user_id.split("@")[0]}
                                    </span>
                                  </p>
                                )}
                                <h1 className="text-4xl font-semibold tracking-tight md:text-5xl">
                                  <Wordmark label="Long Horizon Harness" />
                                </h1>
                                <p className="mx-auto mt-4 max-w-xl text-base text-muted-foreground md:text-lg">
                                  A long-horizon agent that runs in a sandbox
                                  and gets better at its work over time.
                                </p>
                              </div>
                              <div className="mt-6">
                                <StatusStrip contextId={contextId} />
                                <AuthCard
                                  contextId={contextId}
                                  className="mb-2"
                                />
                                <SandboxWarmupBanner
                                  status={sandboxStatus}
                                  className="mb-2"
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
                                  hasPendingPrefix={
                                    pendingUploadNames.length > 0 ||
                                    Boolean(pendingSelection)
                                  }
                                  selectionChip={selectionChip}
                                  fileRefs={fileRefs}
                                  onAddFileRef={addFileRef}
                                  onRemoveFileRef={removeFileRef}
                                  onClearSelection={() =>
                                    setPendingSelection(null)
                                  }
                                />
                              </div>
                              <PromptChips
                                onSend={handleSend}
                                disabled={!client || busy}
                              />
                              <div className="mt-6 flex flex-wrap items-center justify-center gap-2">
                                <div className="inline-flex items-center gap-1.5 rounded-full border border-border/60 bg-card/40 px-3 py-1 text-xs text-muted-foreground">
                                  <Sparkles className="h-3.5 w-3.5 text-primary/70" />
                                  Built with{" "}
                                  <span className="font-mono text-foreground/80">
                                    agents-cli
                                  </span>
                                </div>
                                <span className="inline-flex items-center gap-1.5 text-xs text-muted-foreground/70">
                                  Press{" "}
                                  <kbd className="rounded border bg-muted/40 px-1 font-mono text-[10px] text-muted-foreground">
                                    ⌘K
                                  </kbd>{" "}
                                  for shortcuts
                                </span>
                              </div>
                            </div>
                          </div>
                        </div>
                      ) : (
                        <>
                          {findOpen && (
                            <FindBar
                              messages={messages}
                              onClose={handleFindClose}
                              onActiveMatchChange={handleFindActiveMatch}
                              focusSignal={findFocusSignal}
                            />
                          )}
                          <ConfirmationProvider sender={confirmationSender}>
                            <MessageList
                              messages={messages}
                              contextId={contextId}
                              onResend={resend}
                              onEdit={editAndResend}
                              highlightId={findHighlightId}
                            />
                          </ConfirmationProvider>
                          <StatusStrip contextId={contextId} />
                          <AuthCard
                            contextId={contextId}
                            className="mx-4 mb-2"
                          />
                          <SandboxWarmupBanner
                            status={sandboxStatus}
                            warmError={warmError}
                            className="mx-4 mb-2"
                          />
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
                            hasPendingPrefix={
                              pendingUploadNames.length > 0 ||
                              Boolean(pendingSelection)
                            }
                            selectionChip={selectionChip}
                            fileRefs={fileRefs}
                            onAddFileRef={addFileRef}
                            onRemoveFileRef={removeFileRef}
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
                      defaultSize={26}
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
              <CommandPalette
                open={paletteOpen}
                onOpenChange={setPaletteOpen}
                sessions={sessions}
                activeContextId={contextId}
                onNewChat={onNewChat}
                canExport={messages.length > 0}
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

const selectIteration = (
  d: import("@/lib/horizon-state").HorizonStateResponse,
) => d.state.iteration;

function StatusStrip({ contextId }: { contextId: string | null }) {
  const { data: iteration } = useLhaState(contextId, {
    select: selectIteration,
  });
  if (!contextId) return null;
  if (!iteration) return null;
  return (
    <div className="mx-auto flex w-full max-w-2xl items-center gap-3 px-4 py-1.5 text-[11px] text-muted-foreground">
      <span>iter {iteration}</span>
    </div>
  );
}

interface PromptChip {
  label: string;
  Icon: LucideIcon;
}

const PROMPT_CHIPS: PromptChip[] = [
  { label: "Recap what you remember about me", Icon: Brain },
  { label: "Fetch a public dataset, plot it, save the PNG", Icon: LineChart },
  {
    label:
      "Summarize today's top 3 AI research papers and create an HTML report",
    Icon: Newspaper,
  },
  {
    label: "Every day, analyze new activity in google/adk-python",
    Icon: CalendarClock,
  },
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
      <span className="text-[11px] uppercase tracking-wider text-muted-foreground/70">
        Try a starter
      </span>
      <div className="flex flex-wrap items-center justify-center gap-2">
        {PROMPT_CHIPS.map(({ label, Icon }) => (
          <button
            key={label}
            type="button"
            disabled={disabled}
            onClick={() => onSend(label)}
            className="group/chip inline-flex items-center gap-2 rounded-full border border-border bg-card px-4 py-2.5 text-sm text-foreground/80 shadow-sm transition-all hover:-translate-y-0.5 hover:border-primary/40 hover:bg-primary/5 hover:text-foreground hover:shadow focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 disabled:cursor-not-allowed disabled:opacity-50 disabled:hover:translate-y-0 disabled:hover:shadow-sm"
          >
            <Icon className="h-3.5 w-3.5 text-primary/70 transition-colors group-hover/chip:text-primary" />
            {label}
          </button>
        ))}
      </div>
    </div>
  );
}
