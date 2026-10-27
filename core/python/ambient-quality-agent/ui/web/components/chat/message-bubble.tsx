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

import React, { useEffect, useId, useState } from "react";
import {
  BookOpen,
  Check,
  ChevronRight,
  ExternalLink,
  FileIcon,
  FileText,
  Image as ImageIcon,
  Loader2,
  Music,
  Pencil,
  RotateCcw,
  Scissors,
  Wrench,
  X,
} from "lucide-react";
import { mono, link, textStyle } from "@/lib/typography";
import { cn } from "@/lib/utils";

import { useNow } from "@/lib/now-context";
import type { ChatMessage, MessageSegment } from "./chat-shell";
import { useBusy } from "./busy-context";
import { useConfirmationSender } from "./confirmation-context";
import { CopyButton } from "./copy-button";
import { Markdown } from "./markdown";
import { useViewerOptional } from "./artifact-viewer/viewer-context";
import { isSelectionPayload, type SelectionPayload } from "./workspace-href";
import {
  PatchView,
  ReadFileView,
  TerminalView,
  TOOL_ICONS,
  WriteFileView,
  patchPreview,
  readFilePreview,
  terminalPreview,
  writeFilePreview,
} from "./tool-views";
import {
  RECORD_ROOT_CAUSE_TOOL,
  RootCauseToolResult,
  rootCausePreview,
} from "@/components/insights/root-cause-record";

interface MessageBubbleProps {
  message: ChatMessage;
  contextId?: string | null;
  onResend?: (message: ChatMessage) => void;
  onEdit?: (message: ChatMessage, newText: string) => void;
}

function MessageBubbleInner({
  message,
  contextId = null,
  onResend,
  onEdit,
}: MessageBubbleProps) {
  const hasContent = message.segments.length > 0;
  const isUser = message.role === "user";
  const isSystem = message.role === "system";

  if (isSystem) {
    return (
      <div className="mx-auto max-w-[80%] text-center">
        {message.segments.map((seg, i) =>
          seg.kind === "text" ? (
            <div
              // biome-ignore lint/suspicious/noArrayIndexKey: segments carry no ids, and a message only appends to them, so the position is stable.
              key={`sys-${i}-${seg.text.slice(0, 24)}`}
              className={cn(
                "rounded-md border border-dashed bg-muted/30 px-3 py-1.5",
                textStyle.meta,
              )}
            >
              {seg.text}
            </div>
          ) : null,
        )}
      </div>
    );
  }

  const grouped = groupSegments(message.segments);

  return (
    <div
      className={cn(
        "flex flex-col gap-1.5",
        isUser ? "items-end" : "items-start",
      )}
    >
      {grouped.map((group, gi) => {
        if (group.kind === "toolGroup") {
          // biome-ignore lint/suspicious/noArrayIndexKey: segment groups carry no ids, and a message only appends to them, so the position is stable.
          return <ToolGroup key={`tg-${gi}`} tools={group.tools} />;
        }
        if (group.kind === "confirmGroup") {
          return (
            // biome-ignore lint/suspicious/noArrayIndexKey: segment groups carry no ids, and a message only appends to them, so the position is stable.
            <CombinedPermissionCard key={`cg-${gi}`} items={group.items} />
          );
        }
        const seg = group.segment;
        const i = group.index;
        if (seg.kind === "tool") {
          return (
            <ToolRow
              key={`tool-${seg.callId ?? i}`}
              name={seg.name}
              args={seg.args}
              result={seg.result}
              hasResult={seg.hasResult ?? false}
            />
          );
        }
        if (seg.kind === "data") {
          if (isSelectionPayload(seg.data)) {
            return <SelectionQuote key={`sel-${i}`} selection={seg.data} />;
          }
          return (
            <pre
              key={`data-${i}`}
              className={cn(
                "max-w-full overflow-x-auto rounded-md border bg-muted/40 p-3",
                textStyle.code,
              )}
            >
              <span className="text-muted-foreground">
                data · {seg.mime ?? "application/json"}
              </span>
              {"\n"}
              {JSON.stringify(seg.data, null, 2)}
            </pre>
          );
        }
        if (seg.kind === "file") {
          return <FilePart key={`file-${i}`} file={seg.file} />;
        }
        if (seg.kind === "clarify") {
          return (
            <ClarifyCard
              key={`clarify-${i}`}
              callId={seg.callId}
              question={seg.question}
              choices={seg.choices}
              answer={seg.answer}
            />
          );
        }
        if (seg.kind === "confirmation") {
          const isPermission =
            seg.payload != null &&
            (seg.payload as { kind?: string }).kind === "permission";
          if (isPermission) {
            return (
              <PermissionCard
                key={`perm-${i}`}
                callId={seg.callId}
                payload={seg.payload as unknown as PermissionPayload}
                answer={seg.answer}
              />
            );
          }
          return (
            <ConfirmationCard
              key={`confirm-${i}`}
              callId={seg.callId}
              hint={seg.hint}
              originalCall={seg.originalCall}
              payload={seg.payload}
              answer={seg.answer}
            />
          );
        }
        if (isUser) {
          return (
            <UserTextSegment
              key={`text-${i}`}
              text={seg.text}
              onResend={onResend ? () => onResend(message) : undefined}
              onEdit={
                onEdit ? (newText) => onEdit(message, newText) : undefined
              }
            />
          );
        }
        return (
          <div
            key={`text-${i}`}
            aria-live="polite"
            aria-atomic="false"
            // min-w-0 is load-bearing: as a flex item this wrapper defaults to
            // min-width: auto, which lets long unbreakable content (a model-emitted
            // inline code line, a <pre> with a wide command) burst past the message
            // column. With it, such content wraps/scrolls within the column instead.
            className={cn(
              "group/text relative min-w-0 max-w-full",
              textStyle.body,
            )}
          >
            <Markdown text={seg.text} inverted={false} />
            {seg.text.length > 0 && (
              // pb-2 is a transparent hover bridge: the overlay floats above the
              // text with a small gap, and without it the cursor crosses dead
              // space between text and button — dropping group-hover and hiding
              // the button before it can be clicked.
              <div className="pointer-events-none absolute -top-8 right-0 pb-2 opacity-0 transition-opacity group-hover/text:pointer-events-auto group-hover/text:opacity-100 focus-within:pointer-events-auto focus-within:opacity-100">
                <CopyButton getText={() => seg.text} />
              </div>
            )}
          </div>
        );
      })}
      {!isUser && !message.pending && hasContent && (
        <MessageTimestamp timestampMs={message.createdAt} />
      )}
      {!hasContent && message.pending && (
        <PendingIndicator contextId={contextId} />
      )}
      {hasContent && message.pending && !isUser && (
        <span
          aria-hidden="true"
          className="shimmer inline-block h-3 w-6 rounded-sm opacity-70"
        />
      )}
    </div>
  );
}

// segments is a new array on every reducer update, so reference equality holds.
// `busy` lives in BusyContext now, so toggling it no longer cascades through
// every memoized bubble.
export const MessageBubble = React.memo(MessageBubbleInner, (prev, next) => {
  return (
    prev.message.id === next.message.id &&
    prev.message.segments === next.message.segments &&
    prev.message.pending === next.message.pending &&
    prev.contextId === next.contextId &&
    prev.onResend === next.onResend &&
    prev.onEdit === next.onEdit
  );
});

const UPLOAD_PREFIX_RE =
  /^\[Uploaded to \/workspace\/uploads: ([^\]]+)\]\n?\n?/;

function parseUploadPrefix(
  text: string,
): { names: string[]; rest: string } | null {
  const match = text.match(UPLOAD_PREFIX_RE);
  if (!match) return null;
  const names = match[1]
    .split(",")
    .map((n) => n.trim())
    .filter((n) => n.length > 0);
  if (names.length === 0) return null;
  return { names, rest: text.slice(match[0].length) };
}

function UploadChipsRow({ names }: { names: string[] }) {
  return (
    <div className="flex max-w-[88%] flex-wrap justify-end gap-1.5">
      {names.map((name) => (
        <span
          key={name}
          className={cn(
            "inline-flex items-center gap-1.5 rounded-md border border-border/60 bg-card px-2 py-1 shadow-sm",
            textStyle.meta,
          )}
          title={`/workspace/uploads/${name}`}
        >
          <FileIcon className="h-3 w-3 shrink-0 text-muted-foreground" />
          <span className={cn("max-w-[20ch] truncate", mono)}>{name}</span>
        </span>
      ))}
    </div>
  );
}

function UserTextSegment({
  text,
  onResend,
  onEdit,
}: {
  text: string;
  onResend?: () => void;
  onEdit?: (newText: string) => void;
}) {
  const busy = useBusy();
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(text);

  const upload = parseUploadPrefix(text);
  const displayText = upload ? upload.rest : text;

  if (editing) {
    const trimmed = draft.trim();
    const dirty = trimmed.length > 0 && trimmed !== text.trim();
    return (
      <div className="flex w-full max-w-[88%] flex-col gap-2 rounded-2xl rounded-br-md border border-primary/30 bg-card px-3 py-2.5 shadow-sm">
        <textarea
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          rows={Math.min(8, Math.max(2, draft.split("\n").length))}
          // biome-ignore lint/a11y/noAutofocus: choosing Edit is a request to type in the message.
          autoFocus
          className={cn(
            "w-full resize-y rounded-md border bg-background px-2 py-1.5 placeholder:text-muted-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2",
            textStyle.body,
          )}
        />
        <div className="flex justify-end gap-1.5">
          <button
            type="button"
            onClick={() => {
              setDraft(text);
              setEditing(false);
            }}
            className={cn(
              "rounded-md border bg-background px-3 py-1 transition-colors hover:bg-accent focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2",
              textStyle.label,
              "text-foreground",
            )}
          >
            Cancel
          </button>
          <button
            type="button"
            disabled={!dirty || busy || !onEdit}
            onClick={() => {
              onEdit?.(draft);
              setEditing(false);
            }}
            className={cn(
              "rounded-md border bg-primary px-3 py-1 transition-colors hover:bg-primary/90 disabled:opacity-50 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2",
              textStyle.label,
              "text-primary-foreground",
            )}
          >
            Save & send
          </button>
        </div>
      </div>
    );
  }

  const actionButtons = (
    <>
      {onEdit && (
        <BubbleIconButton
          ariaLabel="Edit message"
          onClick={() => {
            setDraft(text);
            setEditing(true);
          }}
          disabled={busy}
          Icon={Pencil}
        />
      )}
      {onResend && (
        <BubbleIconButton
          ariaLabel="Resend message"
          onClick={onResend}
          disabled={busy}
          Icon={RotateCcw}
        />
      )}
    </>
  );
  // Desktop: hover/focus-reveal overlay positioned just outside the bubble's
  // top edge so it never overlaps the message text. Fully hidden until hover.
  // pb-2 is a transparent hover bridge across the gap to the bubble — without
  // it the cursor crosses dead space and group-hover drops before the buttons
  // can be clicked.
  // Mobile: persistent row below the bubble.
  const desktopActions = (onResend || onEdit) && (
    <div className="pointer-events-none absolute -top-8 right-0 hidden gap-1 pb-2 opacity-0 transition-opacity group-hover/text:pointer-events-auto group-hover/text:opacity-100 focus-within:pointer-events-auto focus-within:opacity-100 md:flex">
      {actionButtons}
    </div>
  );
  const mobileActions = (onResend || onEdit) && (
    <div className="-mt-0.5 flex justify-end gap-0.5 md:hidden">
      {actionButtons}
    </div>
  );

  return (
    <div className="flex w-full flex-col items-end gap-1.5">
      {upload && <UploadChipsRow names={upload.names} />}
      {displayText.length > 0 ? (
        <div className={cn("group/text relative max-w-[88%]", textStyle.body)}>
          <div className="rounded-2xl rounded-br-md bg-primary px-3.5 py-2 text-primary-foreground">
            <Markdown text={displayText} inverted />
          </div>
          {desktopActions}
        </div>
      ) : (
        <div className="group/text relative max-w-[88%]">{desktopActions}</div>
      )}
      {mobileActions}
    </div>
  );
}

function BubbleIconButton({
  ariaLabel,
  onClick,
  disabled,
  Icon,
}: {
  ariaLabel: string;
  onClick: () => void;
  disabled?: boolean;
  Icon: React.ComponentType<{ className?: string }>;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      disabled={disabled}
      aria-label={ariaLabel}
      className="inline-flex h-7 w-7 items-center justify-center rounded-md border bg-card text-muted-foreground shadow-sm transition-colors hover:text-foreground disabled:cursor-not-allowed disabled:opacity-50 max-md:h-8 max-md:w-8 max-md:border-0 max-md:bg-transparent max-md:text-muted-foreground/60 max-md:shadow-none"
    >
      <Icon className="h-3 w-3 max-md:h-3.5 max-md:w-3.5" />
    </button>
  );
}

function PendingIndicator({
  contextId: _contextId,
}: {
  contextId: string | null;
}) {
  const [mountedAt] = useState(() => Date.now());
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const id = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(id);
  }, []);

  const elapsedS = Math.max(0, Math.floor((now - mountedAt) / 1000));
  const elapsedLabel =
    elapsedS < 60
      ? `${elapsedS}s`
      : `${Math.floor(elapsedS / 60)}m${elapsedS % 60}s`;

  return (
    <div className={cn("flex items-center gap-2", textStyle.description)}>
      <Loader2 className="h-3.5 w-3.5 motion-safe:animate-spin" />
      <span>Thinking · {elapsedLabel}</span>
    </div>
  );
}

// Replayed messages (task-to-segments.ts) use a small synthetic counter as
// createdAt purely for ordering — no wall-clock is available. Skip the
// time-ago label below this threshold so we don't render "20598d ago".
const MIN_REAL_TIMESTAMP_MS = 1577836800000; // 2020-01-01

function MessageTimestamp({ timestampMs }: { timestampMs: number }) {
  const now = useNow();
  if (timestampMs < MIN_REAL_TIMESTAMP_MS) return null;
  return (
    <div className={textStyle.meta}>{formatRelative(now - timestampMs)}</div>
  );
}

function formatRelative(deltaMs: number): string {
  if (deltaMs < 45_000) return "just now";
  const mins = Math.round(deltaMs / 60_000);
  if (mins < 60) return `${mins}m ago`;
  const hours = Math.round(mins / 60);
  if (hours < 24) return `${hours}h ago`;
  const days = Math.round(hours / 24);
  return `${days}d ago`;
}

/**
 * A highlighted span, shown above the user's own words.
 *
 * The model-facing framing of the same selection is built server-side from
 * the DataPart, so none of that markup reaches the transcript — the bubble
 * shows what was pointed at, not how the agent was told about it.
 */
function SelectionQuote({ selection }: { selection: SelectionPayload }) {
  const name = selection.path.split("/").pop() || selection.path;
  const full = selection.path;
  const lines =
    selection.start_line === selection.end_line
      ? `line ${selection.start_line}`
      : `lines ${selection.start_line}–${selection.end_line}`;
  const viewer = useViewerOptional();
  return (
    <div
      className={cn(
        "mb-1.5 max-w-full rounded-md border border-border/60 bg-background/40",
        textStyle.meta,
      )}
    >
      <div className="flex items-center gap-1.5 border-b border-border/50 px-2 py-1 text-muted-foreground">
        <Scissors className="h-3 w-3 shrink-0" />
        {viewer ? (
          <button
            type="button"
            onClick={() =>
              viewer.openArtifact({
                name,
                mimeType: "",
                path: full,
                url: `/lha/workspace/download?path=${encodeURIComponent(full)}&inline=1`,
              })
            }
            className={cn("truncate", mono, link.standalone)}
            title={`Open ${full} in the side panel`}
          >
            {name}
          </button>
        ) : (
          <span className={cn("truncate", mono)}>{name}</span>
        )}
        <span className="shrink-0">· {lines}</span>
      </div>
      {/* Whitespace preserved: this is a verbatim excerpt of the file. */}
      <pre
        className={cn(
          "max-h-32 overflow-auto whitespace-pre-wrap break-words px-2 py-1.5",
          textStyle.code,
          "text-muted-foreground",
        )}
      >
        {selection.snippet}
      </pre>
    </div>
  );
}

function FilePart({
  file,
}: {
  file: {
    bytes?: string;
    uri?: string;
    mimeType?: string;
    name?: string;
  };
}) {
  const mime = file.mimeType ?? "application/octet-stream";
  const name = file.name ?? defaultFileName(mime);
  const viewer = useViewerOptional();
  const hasContent = Boolean(file.bytes || file.uri);
  // An attachment carries bytes and a name, never a location, and there is no
  // workspace to look the name up in, so the bytes are all there is.
  const onOpen =
    hasContent && viewer
      ? () =>
          viewer.openArtifact({
            name,
            mimeType: mime,
            bytes: file.bytes,
            url: file.uri,
          })
      : undefined;
  return (
    <AttachmentChip
      name={name}
      mime={mime}
      hasContent={hasContent}
      onOpen={onOpen}
    />
  );
}

// Files never render inline — every attachment is a chip that opens in the
// right-panel artifact viewer (view-only; the chat stays a conversation).
function attachmentIcon(mime: string) {
  if (mime.startsWith("image/")) return ImageIcon;
  if (mime.startsWith("audio/")) return Music;
  if (mime === "application/pdf" || mime.startsWith("text/")) return FileText;
  return FileIcon;
}

function AttachmentChip({
  name,
  mime,
  hasContent,
  onOpen,
}: {
  name: string;
  mime: string;
  hasContent: boolean;
  onOpen?: () => void;
}) {
  const Icon = attachmentIcon(mime);
  if (!onOpen) {
    return (
      <div
        className={cn(
          "inline-flex max-w-[min(320px,100%)] items-center gap-2 rounded-md border bg-muted/40 px-2.5 py-1.5",
          textStyle.meta,
        )}
      >
        <Icon className="h-4 w-4 shrink-0" />
        <span className="min-w-0 flex-1 truncate" title={name}>
          {name}
        </span>
        {!hasContent && <span className="shrink-0">(no content)</span>}
      </div>
    );
  }
  return (
    <button
      type="button"
      onClick={onOpen}
      title={`Open ${name}`}
      className={cn(
        "inline-flex max-w-[min(320px,100%)] items-center gap-2 rounded-md border bg-card px-2.5 py-1.5 transition-colors hover:bg-accent focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring",
        textStyle.meta,
        "text-foreground",
      )}
    >
      <Icon className="h-4 w-4 shrink-0 text-muted-foreground" />
      <span className="min-w-0 flex-1 truncate">{name}</span>
      <ExternalLink className="h-3.5 w-3.5 shrink-0 text-muted-foreground/70" />
    </button>
  );
}

function defaultFileName(mime: string): string {
  const ext = mime.split("/")[1] ?? "bin";
  return `attachment.${ext}`;
}

function ClarifyCard({
  callId,
  question,
  choices,
  answer: resolved,
}: {
  callId: string | null;
  question: string;
  choices: string[];
  answer?: { confirmed: boolean; text: string | null };
}) {
  const send = useConfirmationSender();
  const [answer, setAnswer] = useState("");
  if (resolved) {
    return (
      <div className="flex max-w-[88%] flex-col gap-1.5 rounded-lg border bg-card px-3.5 py-2.5">
        <div className={textStyle.description}>{question}</div>
        <div className={cn(textStyle.body, "font-medium")}>
          {resolved.confirmed && resolved.text !== null
            ? `You answered: ${resolved.text}`
            : "Dismissed"}
        </div>
      </div>
    );
  }
  if (choices.length > 0) {
    return (
      <div className="flex max-w-[88%] flex-col gap-2 rounded-lg border bg-card px-3.5 py-2.5">
        <div className={textStyle.itemTitle}>{question}</div>
        <div className="flex flex-wrap gap-1.5">
          {choices.map((c) => (
            <button
              key={c}
              type="button"
              onClick={() => {
                if (resolved) return;
                send({ callId, confirmed: true, payload: { choice: c } });
              }}
              className={cn(
                "rounded-md border bg-background px-2.5 py-1 transition-colors hover:bg-accent focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2",
                textStyle.label,
                "text-foreground",
              )}
            >
              {c}
            </button>
          ))}
        </div>
      </div>
    );
  }
  const trimmed = answer.trim();
  return (
    <div className="flex w-full max-w-[88%] flex-col gap-2 rounded-lg border bg-card px-3.5 py-2.5">
      <div className={textStyle.itemTitle}>{question}</div>
      <textarea
        value={answer}
        onChange={(e) => setAnswer(e.target.value)}
        rows={2}
        className={cn(
          "resize-y rounded-md border bg-background px-2 py-1.5 placeholder:text-muted-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2",
          textStyle.body,
        )}
      />
      <div className="flex justify-end">
        <button
          type="button"
          disabled={trimmed.length === 0}
          onClick={() => {
            if (resolved) return;
            send({
              callId,
              confirmed: true,
              payload: { answer: trimmed },
            });
          }}
          className={cn(
            "rounded-md border bg-primary px-3 py-1 transition-colors hover:bg-primary/90 disabled:opacity-50 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2",
            textStyle.label,
            "text-primary-foreground",
          )}
        >
          Send
        </button>
      </div>
    </div>
  );
}

function ConfirmationCard({
  callId,
  hint,
  originalCall,
  payload,
  answer: resolved,
}: {
  callId: string | null;
  hint: string;
  originalCall: { name: string; args: Record<string, unknown> | null } | null;
  payload: Record<string, unknown> | null;
  answer?: { confirmed: boolean; text: string | null };
}) {
  const send = useConfirmationSender();
  const hintId = useId();
  const toolPill = originalCall && (
    <div
      className={cn(
        "inline-flex w-fit items-center gap-1.5 rounded-full border bg-muted/40 px-2 py-0.5",
        textStyle.meta,
      )}
    >
      <Wrench className="h-3 w-3 text-primary/70" />
      <span className={cn(mono, "text-foreground")}>{originalCall.name}</span>
    </div>
  );
  const goalPreview = originalCall?.name === "set_goal" && (
    <GoalPreview goal={String(originalCall.args?.goal ?? "")} />
  );
  if (resolved) {
    return (
      <div className="flex max-w-[88%] flex-col gap-1.5 rounded-lg border bg-card px-3.5 py-2.5">
        <div className={textStyle.description}>{hint}</div>
        {goalPreview}
        {toolPill}
        <div
          className={cn(
            "flex items-center gap-1",
            textStyle.body,
            "font-medium",
            resolved.confirmed ? "text-foreground" : "text-muted-foreground",
          )}
        >
          {resolved.confirmed && (
            <Check aria-hidden="true" className="h-3.5 w-3.5 shrink-0" />
          )}
          {resolved.confirmed ? "Approved" : "Declined"}
        </div>
      </div>
    );
  }
  return (
    <div
      role="alertdialog"
      aria-labelledby={hintId}
      className="flex max-w-[88%] flex-col gap-2 rounded-lg border border-l-4 border-l-primary bg-card px-3.5 py-2.5"
    >
      <div id={hintId} className={textStyle.itemTitle}>
        {hint}
      </div>
      {goalPreview}
      {toolPill}
      <div className="flex justify-end gap-1.5">
        <button
          type="button"
          onClick={() => {
            if (resolved) return;
            send({ callId, confirmed: false, payload });
          }}
          className={cn(
            "rounded-md border bg-background px-3 py-1 transition-colors hover:bg-accent focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2",
            textStyle.label,
            "text-foreground",
          )}
        >
          Decline
        </button>
        <button
          type="button"
          onClick={() => {
            if (resolved) return;
            send({ callId, confirmed: true, payload });
          }}
          className={cn(
            "rounded-md border bg-primary px-3 py-1 transition-colors hover:bg-primary/90 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2",
            textStyle.label,
            "text-primary-foreground",
          )}
        >
          Approve
        </button>
      </div>
    </div>
  );
}

/** The text `set_goal` will save, as the user approves it: every line, since
 *  the approval is for exactly this text. An empty goal removes it. */
function GoalPreview({ goal }: { goal: string }) {
  if (!goal.trim()) {
    return (
      <p className={textStyle.description}>
        Investigations run without a goal until you save another. The current
        goal stays in Previous versions on the Configuration page.
      </p>
    );
  }
  return (
    // A div, not a pre: the goal is prose, and pre takes the mono family.
    // biome-ignore lint/a11y/useAriaPropsSupportedByRole: the label lets tests find the preview; a role to support it would change what screen readers announce.
    <div
      aria-label="Goal to save"
      className={cn(
        "max-h-64 overflow-auto whitespace-pre-wrap break-words rounded-md border bg-muted/40 p-2.5",
        textStyle.body,
      )}
    >
      {goal}
    </div>
  );
}

interface PermissionPayload {
  kind: "permission";
  toolName: string;
  summary: string;
  proposedRule: Record<string, unknown>;
  choices: string[];
}

function CommandPreview({ id, text }: { id?: string; text: string }) {
  // Long/multi-line commands collapse to a one-line preview so the approval card
  // stays compact; expand for the full, scrollable command.
  if (text.length <= 72 && !text.includes("\n")) {
    return (
      <div
        id={id}
        className={cn("whitespace-pre-wrap break-words", textStyle.code)}
      >
        {text}
      </div>
    );
  }
  return (
    <details className="group min-w-0">
      <summary
        id={id}
        className="flex cursor-pointer list-none items-center gap-1 text-muted-foreground hover:text-foreground [&::-webkit-details-marker]:hidden"
      >
        <ChevronRight className="h-3 w-3 shrink-0 transition-transform group-open:rotate-90" />
        <span className={cn("truncate", textStyle.code)}>{text}</span>
      </summary>
      <pre
        className={cn(
          "mt-1.5 max-h-48 overflow-auto whitespace-pre-wrap break-words rounded-md bg-muted/40 p-2",
          textStyle.code,
        )}
      >
        {text}
      </pre>
    </details>
  );
}

function PermissionCard({
  callId,
  payload,
  answer: resolved,
}: {
  callId: string | null;
  payload: PermissionPayload;
  answer?: { confirmed: boolean; text: string | null };
}) {
  const send = useConfirmationSender();
  const hintId = useId();
  const toolPill = (
    <div
      className={cn(
        "inline-flex w-fit items-center gap-1.5 rounded-full border bg-muted/40 px-2 py-0.5",
        textStyle.meta,
      )}
    >
      <Wrench className="h-3 w-3 text-primary/70" />
      <span className={cn(mono, "text-foreground")}>{payload.toolName}</span>
    </div>
  );
  if (resolved) {
    // Once answered the turn moves on, so collapse to a single compact line
    // (status + the chosen outcome). Still expandable to re-read the full
    // command after the fact.
    return (
      <details className="group min-w-0 max-w-[88%]">
        <summary
          className={cn(
            "flex cursor-pointer list-none items-center gap-2 rounded-md border bg-card/60 px-2.5 py-1 hover:bg-accent/40 [&::-webkit-details-marker]:hidden",
            textStyle.meta,
          )}
        >
          <ChevronRight className="h-3 w-3 shrink-0 text-muted-foreground transition-transform group-open:rotate-90" />
          <Wrench className="h-3 w-3 shrink-0 text-muted-foreground" />
          {resolved.confirmed ? (
            <Check
              role="img"
              aria-label="Approved"
              className="h-3 w-3 shrink-0 text-primary"
            />
          ) : (
            <X
              role="img"
              aria-label="Declined"
              className="h-3 w-3 shrink-0 text-muted-foreground"
            />
          )}
          <span
            className={cn(
              "truncate",
              textStyle.label,
              resolved.confirmed ? "text-primary" : "text-muted-foreground",
            )}
          >
            {resolved.text || payload.summary}
          </span>
        </summary>
        <pre
          className={cn(
            "mt-1.5 max-h-48 overflow-auto whitespace-pre-wrap break-words rounded-md bg-muted/40 p-2",
            textStyle.code,
          )}
        >
          {payload.summary}
        </pre>
      </details>
    );
  }
  return (
    <div
      role="alertdialog"
      aria-labelledby={hintId}
      className="flex max-w-[88%] flex-col gap-2 rounded-lg border border-l-4 border-l-primary bg-card px-3.5 py-2.5"
    >
      <CommandPreview id={hintId} text={payload.summary} />
      {toolPill}
      <div className="flex flex-wrap justify-end gap-1.5">
        {payload.choices.map((label, idx) => {
          // cancel is the trailing choice (server's ordered_outcomes contract).
          const isDecline = idx === payload.choices.length - 1;
          return (
            <button
              key={label}
              type="button"
              onClick={() =>
                send({
                  callId,
                  confirmed: !isDecline,
                  payload: { choice: label },
                })
              }
              className={cn(
                "rounded-md border px-3 py-1 transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2",
                textStyle.label,
                isDecline
                  ? "bg-background text-foreground hover:bg-accent"
                  : "bg-primary text-primary-foreground hover:bg-primary/90",
              )}
            >
              {label}
            </button>
          );
        })}
      </div>
    </div>
  );
}

function CombinedPermissionCard({ items }: { items: ConfirmItem[] }) {
  const send = useConfirmationSender();
  // One decision for the whole batch, sent in a single resume. We send the
  // outcome KEY (not the single card's per-label `choice`) because labels differ
  // per command while the outcome order is identical for every permission card.
  const batch = (confirmed: boolean, outcome: string) =>
    send(
      items.map((it) => ({
        callId: it.callId,
        confirmed,
        payload: { outcome },
      })),
    );
  return (
    // biome-ignore lint/a11y/useSemanticElements: a fieldset brings UA min-inline-size and legend handling into this flex card.
    <div
      role="group"
      className="flex max-w-[88%] flex-col gap-2 rounded-lg border border-l-4 border-l-primary bg-card px-3.5 py-2.5"
    >
      <div className={textStyle.itemTitle}>
        {items.length} actions need approval
      </div>
      <ul className="flex flex-col gap-1">
        {items.map((it, k) => (
          <li
            key={it.callId ?? k}
            className={cn(textStyle.code, "text-muted-foreground")}
          >
            {it.payload.summary}
          </li>
        ))}
      </ul>
      <div className="flex flex-wrap justify-end gap-1.5">
        <button
          type="button"
          onClick={() => batch(true, "proceed_once")}
          className={cn(
            "rounded-md border bg-primary px-3 py-1 hover:bg-primary/90",
            textStyle.label,
            "text-primary-foreground",
          )}
        >
          Yes, once
        </button>
        <button
          type="button"
          onClick={() => batch(true, "proceed_always")}
          className={cn(
            "rounded-md border bg-primary px-3 py-1 hover:bg-primary/90",
            textStyle.label,
            "text-primary-foreground",
          )}
        >
          This chat
        </button>
        <button
          type="button"
          onClick={() => batch(false, "cancel")}
          className={cn(
            "rounded-md border bg-background px-3 py-1 hover:bg-accent",
            textStyle.label,
            "text-foreground",
          )}
        >
          Decline
        </button>
      </div>
    </div>
  );
}

type ToolEntry = {
  callId: string | null;
  name: string;
  args: Record<string, unknown> | null;
  result: unknown;
  hasResult: boolean;
};

type ConfirmItem = { callId: string | null; payload: PermissionPayload };

type SegmentGroup =
  | { kind: "toolGroup"; tools: ToolEntry[] }
  | { kind: "confirmGroup"; items: ConfirmItem[] }
  | { kind: "segment"; segment: MessageSegment; index: number };

function isPendingPermission(
  seg: MessageSegment,
): seg is Extract<MessageSegment, { kind: "confirmation" }> {
  return (
    seg.kind === "confirmation" &&
    seg.answer === undefined &&
    seg.payload != null &&
    (seg.payload as { kind?: string }).kind === "permission"
  );
}

function groupSegments(segments: MessageSegment[]): SegmentGroup[] {
  const out: SegmentGroup[] = [];
  let current: ToolEntry[] | null = null;
  for (let i = 0; i < segments.length; i++) {
    const seg = segments[i];
    // Group consecutive tool calls, excluding root-cause recordings so the final
    // diagnosis renders directly instead of collapsing into a group.
    if (seg.kind === "tool" && seg.name !== RECORD_ROOT_CAUSE_TOOL) {
      if (!current) {
        current = [];
        out.push({ kind: "toolGroup", tools: current });
      }
      current.push({
        callId: seg.callId,
        name: seg.name,
        args: seg.args,
        result: seg.result,
        hasResult: seg.hasResult ?? false,
      });
      continue;
    }
    current = null;
    if (isPendingPermission(seg)) {
      const items: ConfirmItem[] = [];
      let j = i;
      while (j < segments.length) {
        const s = segments[j];
        if (!isPendingPermission(s)) break;
        items.push({
          callId: s.callId,
          payload: s.payload as unknown as PermissionPayload,
        });
        j++;
      }
      if (items.length >= 2) {
        out.push({ kind: "confirmGroup", items });
        i = j - 1;
        continue;
      }
    }
    out.push({ kind: "segment", segment: seg, index: i });
  }
  return out;
}

function ToolGroup({ tools }: { tools: ToolEntry[] }) {
  const [open, setOpen] = useState(false);
  // Solo tools render as a bare row — wrapping a single call in an outer
  // expander would be more chrome than information.
  if (tools.length === 1) {
    const t = tools[0];
    return (
      <ToolRow
        name={t.name}
        args={t.args}
        result={t.result}
        hasResult={t.hasResult}
      />
    );
  }
  const pending = tools.filter((t) => !t.hasResult).length;
  return (
    <div className="flex w-full max-w-full flex-col gap-1">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className={cn(
          "group/tg flex w-fit max-w-full items-center gap-2 rounded-md px-2 py-1 transition-colors hover:bg-accent/50 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 max-md:py-1.5",
          textStyle.meta,
        )}
        aria-expanded={open}
      >
        <Wrench className="h-3.5 w-3.5 shrink-0 text-muted-foreground/60" />
        <span className={cn(textStyle.label, "text-foreground")}>
          {tools.length} tool calls
        </span>
        {pending > 0 && (
          <Loader2 className="h-3 w-3 shrink-0 motion-safe:animate-spin text-muted-foreground/60" />
        )}
        <ChevronRight
          className={cn(
            "h-3.5 w-3.5 shrink-0 text-muted-foreground/40 transition-transform group-hover/tg:text-muted-foreground/70",
            open && "rotate-90",
          )}
        />
      </button>
      {open && (
        <div className="flex max-h-64 w-full max-w-full flex-col gap-1 overflow-y-auto rounded-md border bg-muted/20 p-2">
          {tools.map((t, i) => (
            <ToolRow
              key={`tg-${t.callId ?? `${t.name}-${i}`}`}
              name={t.name}
              args={t.args}
              result={t.result}
              hasResult={t.hasResult}
            />
          ))}
        </div>
      )}
    </div>
  );
}

function ToolRow({
  name,
  args,
  result,
  hasResult,
}: {
  name: string;
  args: Record<string, unknown> | null;
  result: unknown;
  hasResult: boolean;
}) {
  // Expand root-cause recordings by default so the diagnosis is immediately visible.
  const [open, setOpen] = useState(name === RECORD_ROOT_CAUSE_TOOL);
  const isLoadSkill = name === "load_skill";
  // The tool's real arg is skill_name, not name (final-review Fix 9) — see
  // horizon/tools/skill_toolset.py's declared schema.
  const skillName =
    isLoadSkill && typeof args?.skill_name === "string"
      ? args.skill_name
      : null;
  const richPreview = pickRichPreview(name, args);
  const preview = isLoadSkill
    ? (skillName ?? "skill")
    : (richPreview ?? formatArgsPreview(args));
  const Icon = isLoadSkill ? BookOpen : (TOOL_ICONS[name] ?? Wrench);

  return (
    <div className="flex w-full max-w-full flex-col gap-1">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className={cn(
          "group/tool flex w-fit max-w-full items-center gap-2 rounded-md px-2 py-1 transition-colors hover:bg-accent/50 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 max-md:py-1.5",
          textStyle.meta,
        )}
        aria-expanded={open}
      >
        <Icon className="h-3.5 w-3.5 shrink-0 text-muted-foreground/60" />
        <span className={cn(textStyle.label, mono, "text-foreground")}>
          {name}
        </span>
        {preview && <span className={cn("truncate", mono)}>{preview}</span>}
        {!hasResult && (
          <Loader2 className="h-3 w-3 shrink-0 motion-safe:animate-spin text-muted-foreground/60" />
        )}
        <ChevronRight
          className={cn(
            "h-3.5 w-3.5 shrink-0 text-muted-foreground/40 transition-transform group-hover/tool:text-muted-foreground/70",
            open && "rotate-90",
          )}
        />
      </button>
      {open && (
        <div className="flex w-full max-w-full flex-col gap-1.5 pl-4">
          <RichToolBody
            name={name}
            args={args}
            result={result}
            hasResult={hasResult}
          />
        </div>
      )}
    </div>
  );
}

function pickRichPreview(
  name: string,
  args: Record<string, unknown> | null,
): string | null {
  switch (name) {
    case "edit":
      return patchPreview(args);
    case "write":
      return writeFilePreview(args);
    case "read":
      return readFilePreview(args);
    case "bash":
      return terminalPreview(args);
    case RECORD_ROOT_CAUSE_TOOL:
      return rootCausePreview(args);
    default:
      return null;
  }
}

function RichToolBody({
  name,
  args,
  result,
  hasResult,
}: {
  name: string;
  args: Record<string, unknown> | null;
  result: unknown;
  hasResult: boolean;
}) {
  if (name === "edit") return <PatchView args={args} result={result} />;
  if (name === "write") {
    return <WriteFileView args={args} result={result} />;
  }
  if (name === "read") {
    return <ReadFileView args={args} result={result} hasResult={hasResult} />;
  }
  if (name === "bash") {
    return <TerminalView args={args} result={result} hasResult={hasResult} />;
  }
  if (name === RECORD_ROOT_CAUSE_TOOL) {
    return <RootCauseToolResult result={result} hasResult={hasResult} />;
  }
  return (
    <>
      <ToolPayload label="Args" data={args} />
      {hasResult ? (
        <ToolPayload label="Result" data={result} />
      ) : (
        <div className={cn("flex items-center gap-1.5", textStyle.meta)}>
          <Loader2 className="h-3 w-3 motion-safe:animate-spin" />
          Awaiting result…
        </div>
      )}
    </>
  );
}

function ToolPayload({ label, data }: { label: string; data: unknown }) {
  const text = formatPayload(data);
  const truncated = text.length > 2000;
  const display = truncated ? text.slice(0, 2000) + "\n… (truncated)" : text;
  return (
    <div className="flex w-full max-w-full flex-col gap-0.5">
      <span className={textStyle.label}>{label}</span>
      <pre
        className={cn(
          "max-w-full overflow-x-auto rounded-md border bg-muted/40 p-2",
          textStyle.code,
        )}
      >
        {display}
      </pre>
    </div>
  );
}

function formatPayload(data: unknown): string {
  if (data === null || data === undefined) return String(data);
  if (typeof data === "string") return data;
  try {
    return JSON.stringify(data, null, 2);
  } catch {
    return String(data);
  }
}

function formatArgsPreview(args: Record<string, unknown> | null): string {
  if (!args) return "";
  const keys = Object.keys(args);
  if (keys.length === 0) return "";
  const parts: string[] = [];
  for (const k of keys.slice(0, 2)) {
    const v = args[k];
    const s = typeof v === "string" ? v : JSON.stringify(v);
    const short = s.length > 40 ? s.slice(0, 39) + "…" : s;
    parts.push(`${k}=${short.replace(/\n/g, " ")}`);
  }
  return parts.join(", ");
}
