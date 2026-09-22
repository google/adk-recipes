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
  type ChangeEvent,
  type ClipboardEvent,
  type DragEvent,
  type FormEvent,
  type KeyboardEvent,
  memo,
  useEffect,
  useRef,
  useState,
} from "react";
import { Textarea } from "@/components/ui/textarea";
import { Button } from "@/components/ui/button";
import {
  ArrowUp,
  FileIcon,
  Paperclip,
  Scissors,
  Square,
  X,
} from "lucide-react";
import { cn } from "@/lib/utils";
import { onFocusComposer } from "@/lib/composer-focus";
import {
  FILEREF_DRAG_MIME,
  parseFileRefDrag,
  type FileRef,
} from "./workspace-href";

export interface InputAttachment {
  /** Stable per-attachment id used as React key + remove handle. */
  id: string;
  /** Display name shown in the chip; falls back to a synthesized name. */
  name: string;
  /** MIME type, e.g. "image/png". Guessed by the browser when omitted. */
  mimeType: string;
  /** Raw bytes for size reporting and base64 conversion. */
  file: File;
  /** Data URL used for inline image thumbnails — null for non-images. */
  previewUrl: string | null;
}

export interface InputBoxProps {
  value: string;
  onValueChange: (value: string) => void;
  onSend: (text: string) => void;
  onStop?: () => void;
  disabled?: boolean;
  busy?: boolean;
  attachments: InputAttachment[];
  onAddFiles: (files: File[]) => void;
  onRemoveAttachment: (id: string) => void;
  attachmentError: string | null;
  // Lets the parent enable Send even when text and attachments are both empty
  // — e.g. the sidebar dropzone has queued an upload that will be prepended
  // as `[Uploaded to /workspace: …]` on the next submit.
  hasPendingPrefix?: boolean;
  // Text highlighted in the artifact panel's Source view. Rendered as a chip
  // because the browser stops painting the native highlight once focus moves
  // here, leaving no other cue that a span is held.
  selectionChip?: { label: string } | null;
  onClearSelection?: () => void;
  // Files dragged in from the workspace tree. References, not uploads: the
  // bytes are already on disk, so only the path travels.
  fileRefs?: FileRef[];
  onAddFileRef?: (ref: FileRef) => void;
  onRemoveFileRef?: (path: string) => void;
  // Messages typed while the agent was busy, awaiting flush. Rendered as a
  // removable stack above the composer.
  queued: string[];
  onRemoveQueued: (index: number) => void;
}

function InputBoxInner({
  value,
  onValueChange,
  onSend,
  onStop,
  disabled,
  busy,
  attachments,
  onAddFiles,
  onRemoveAttachment,
  attachmentError,
  hasPendingPrefix,
  selectionChip,
  onClearSelection,
  fileRefs,
  onAddFileRef,
  onRemoveFileRef,
  queued,
  onRemoveQueued,
}: InputBoxProps) {
  const fileInputRef = useRef<HTMLInputElement>(null);
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const wasBusyRef = useRef(false);
  const autosizeRafRef = useRef<number | null>(null);
  // Browser-specific: Safari fires keydown after compositionend with stale
  // isComposing=true; Chrome on Linux can fire keydown during composition with
  // isComposing=false. Tracking the boundary ourselves double-guards Enter.
  const isComposingRef = useRef(false);

  // Stop button unmounts on busy→idle; native focus lands on <body>. Pull it back.
  useEffect(() => {
    if (wasBusyRef.current && !busy) {
      if (document.activeElement === document.body) {
        textareaRef.current?.focus();
      }
    }
    wasBusyRef.current = !!busy;
  }, [busy]);

  useEffect(() => {
    return () => {
      if (autosizeRafRef.current != null) {
        cancelAnimationFrame(autosizeRafRef.current);
      }
    };
  }, []);

  useEffect(
    () =>
      onFocusComposer(() => {
        textareaRef.current?.focus();
      }),
    [],
  );

  // Autosize on every value change — typing and programmatic alike (e.g. the
  // queue restored into the composer on Stop). Coalesce the scrollHeight read
  // (a sync layout) into one rAF so fast typists measure once per paint.
  useEffect(() => {
    const el = textareaRef.current;
    if (!el) return;
    if (autosizeRafRef.current != null)
      cancelAnimationFrame(autosizeRafRef.current);
    autosizeRafRef.current = requestAnimationFrame(() => {
      autosizeRafRef.current = null;
      el.style.height = "auto";
      el.style.height = Math.min(el.scrollHeight, 200) + "px";
    });
  }, [value]);

  const submit = (e?: FormEvent) => {
    e?.preventDefault();
    if (disabled) return;
    const text = value.trim();
    if (
      !text &&
      attachments.length === 0 &&
      (fileRefs?.length ?? 0) === 0 &&
      !hasPendingPrefix
    )
      return;
    onSend(text);
    onValueChange("");
    if (textareaRef.current) {
      textareaRef.current.style.height = "auto";
    }
  };

  const handleKeyDown = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    const composing = e.nativeEvent.isComposing || isComposingRef.current;
    if (e.key === "Enter" && !e.shiftKey && !composing) {
      e.preventDefault();
      submit();
      return;
    }
    // Backspace at an empty composer pops the most-recent attachment chip —
    // matches Slack / GitHub behavior so power users can clear by feel.
    if (e.key === "Backspace" && value.length === 0 && attachments.length > 0) {
      e.preventDefault();
      onRemoveAttachment(attachments[attachments.length - 1].id);
    }
  };

  const onFilePicked = (e: ChangeEvent<HTMLInputElement>) => {
    const list = e.target.files;
    if (!list) return;
    onAddFiles(Array.from(list));
    // Reset so picking the same file twice in a row still fires onChange.
    e.target.value = "";
  };

  const onPaste = (e: ClipboardEvent<HTMLTextAreaElement>) => {
    const items = e.clipboardData?.items;
    if (!items) return;
    const files: File[] = [];
    for (const item of Array.from(items)) {
      if (item.kind === "file") {
        const f = item.getAsFile();
        if (f) files.push(f);
      }
    }
    if (files.length > 0) {
      e.preventDefault();
      onAddFiles(files);
    }
  };

  const canSend =
    !disabled &&
    (value.trim().length > 0 ||
      attachments.length > 0 ||
      (fileRefs?.length ?? 0) > 0 ||
      !!hasPendingPrefix);

  const [isDraggingFiles, setIsDraggingFiles] = useState(false);
  const dragHasFiles = (e: DragEvent<HTMLFormElement>) =>
    Array.from(e.dataTransfer?.types ?? []).includes("Files");
  const dragHasWorkspaceFile = (e: DragEvent<HTMLFormElement>) =>
    Array.from(e.dataTransfer?.types ?? []).includes(FILEREF_DRAG_MIME);
  const onDragOver = (e: DragEvent<HTMLFormElement>) => {
    if (dragHasWorkspaceFile(e)) {
      e.preventDefault();
      e.stopPropagation();
      setIsDraggingFiles(true);
      return;
    }
    if (!dragHasFiles(e)) return;
    e.preventDefault();
    e.stopPropagation(); // don't let the shell-level workspace dropzone also fire
    setIsDraggingFiles(true);
  };
  const onDragLeave = (e: DragEvent<HTMLFormElement>) => {
    if (e.currentTarget.contains(e.relatedTarget as Node)) return;
    setIsDraggingFiles(false);
  };
  const onDrop = (e: DragEvent<HTMLFormElement>) => {
    if (dragHasWorkspaceFile(e)) {
      e.preventDefault();
      e.stopPropagation();
      setIsDraggingFiles(false);
      const ref = parseFileRefDrag(e.dataTransfer.getData(FILEREF_DRAG_MIME));
      if (ref) onAddFileRef?.(ref);
      return;
    }
    if (!dragHasFiles(e)) return;
    e.preventDefault();
    e.stopPropagation();
    setIsDraggingFiles(false);
    const files = Array.from(e.dataTransfer.files);
    if (files.length) onAddFiles(files);
  };

  return (
    <div className="shrink-0 border-t bg-background pb-[env(safe-area-inset-bottom)]">
      <div className="mx-auto w-full max-w-2xl px-4 py-3">
        <form
          onSubmit={submit}
          onDragOver={onDragOver}
          onDragLeave={onDragLeave}
          onDrop={onDrop}
          className="relative flex flex-col gap-1.5 rounded-2xl border bg-card p-1.5 shadow-sm focus-within:ring-1 focus-within:ring-ring"
        >
          {isDraggingFiles && (
            <div className="pointer-events-none absolute inset-0 z-10 flex items-center justify-center rounded-2xl border-2 border-dashed border-primary/60 bg-primary/5 text-xs font-medium text-primary">
              Drop to attach
            </div>
          )}
          {queued.length > 0 && (
            <div className="flex flex-col gap-1 px-1.5 pt-1">
              {queued.map((q, i) => (
                <div
                  key={`${i}:${q}`}
                  className="flex items-center gap-1.5 rounded-md border bg-muted/40 py-1 pl-2 pr-1 text-xs text-muted-foreground"
                >
                  <span className="flex-1 truncate whitespace-pre-wrap">
                    {q}
                  </span>
                  <button
                    type="button"
                    onClick={() => onRemoveQueued(i)}
                    className="inline-flex h-5 w-5 shrink-0 items-center justify-center rounded text-muted-foreground transition-colors hover:bg-destructive/20 hover:text-destructive"
                    aria-label={`Remove queued message ${i + 1}`}
                  >
                    <X className="h-3 w-3" />
                  </button>
                </div>
              ))}
            </div>
          )}

          {selectionChip && (
            <div className="px-1.5 pt-1">
              <span className="inline-flex max-w-full items-center gap-1.5 rounded-md border border-border/60 px-2 py-0.5 text-[11px] text-muted-foreground">
                <Scissors className="h-3 w-3 shrink-0" />
                <span className="truncate">{selectionChip.label}</span>
                <button
                  type="button"
                  aria-label="Clear selection"
                  onClick={onClearSelection}
                  className="rounded p-0.5 hover:bg-muted"
                >
                  <X className="h-3 w-3" />
                </button>
              </span>
            </div>
          )}

          {(fileRefs?.length ?? 0) > 0 && (
            <div className="flex flex-wrap gap-1.5 px-1.5 pt-1">
              {fileRefs?.map((r) => (
                <FileRefChip
                  key={r.path}
                  fileRef={r}
                  onRemove={() => onRemoveFileRef?.(r.path)}
                />
              ))}
            </div>
          )}

          {attachments.length > 0 && (
            <div className="flex flex-wrap gap-1.5 px-1.5 pt-1">
              {attachments.map((a) => (
                <AttachmentChip
                  key={a.id}
                  attachment={a}
                  onRemove={() => onRemoveAttachment(a.id)}
                />
              ))}
            </div>
          )}

          {attachmentError && (
            <div className="px-2 pt-0.5 text-[11px] text-destructive">
              {attachmentError}
            </div>
          )}

          <div className="flex items-end gap-1.5">
            <Button
              type="button"
              variant="ghost"
              size="icon"
              onClick={() => fileInputRef.current?.click()}
              disabled={disabled || busy}
              className="h-9 w-9 shrink-0 rounded-full text-muted-foreground hover:text-foreground max-md:h-11 max-md:w-11"
              aria-label="Attach files"
            >
              <Paperclip className="h-4 w-4" />
            </Button>
            <input
              ref={fileInputRef}
              type="file"
              multiple
              hidden
              onChange={onFilePicked}
              accept="image/*,audio/*,application/pdf,text/*,.json,.md,.csv,.log,.yaml,.yml,.xml,.html,.tsv"
            />
            <Textarea
              ref={textareaRef}
              value={value}
              onChange={(e) => onValueChange(e.target.value)}
              onKeyDown={handleKeyDown}
              onCompositionStart={() => {
                isComposingRef.current = true;
              }}
              onCompositionEnd={() => {
                isComposingRef.current = false;
              }}
              onPaste={onPaste}
              placeholder={
                disabled
                  ? "Connecting…"
                  : busy
                    ? "Reply will be queued…"
                    : attachments.length > 0
                      ? "Add a message (optional)"
                      : "Ask anything…"
              }
              disabled={disabled}
              rows={1}
              className="min-h-[36px] flex-1 resize-none overflow-y-auto border-0 bg-transparent px-2.5 py-1.5 text-sm shadow-none focus-visible:ring-0 focus-visible:ring-offset-0"
            />
            {busy && onStop ? (
              <Button
                type="button"
                variant="secondary"
                size="icon"
                onClick={onStop}
                className="h-9 w-9 rounded-full max-md:h-11 max-md:w-11"
                aria-label="Stop generating"
              >
                <Square className="h-3.5 w-3.5 fill-current" />
              </Button>
            ) : (
              <Button
                type="submit"
                variant="lha"
                size="icon"
                disabled={!canSend}
                className="h-9 w-9 rounded-full max-md:h-11 max-md:w-11"
                aria-label="Send message"
                title="Send (Enter) · Cmd+K for command palette"
              >
                <ArrowUp className="h-4 w-4" />
              </Button>
            )}
          </div>
        </form>
      </div>
    </div>
  );
}

export const InputBox = memo(InputBoxInner);

/** Same shape as an upload chip; it is a pointer, not a copy. */
function FileRefChip({
  fileRef,
  onRemove,
}: {
  fileRef: FileRef;
  onRemove: () => void;
}) {
  return (
    <div className="group/chip relative flex items-center gap-1.5 rounded-md border bg-muted/50 py-1 pl-1.5 pr-6 text-xs text-foreground">
      <FileIcon className="h-3.5 w-3.5 text-muted-foreground" />
      <div className="flex flex-col leading-tight">
        <span className="max-w-[200px] truncate font-medium">
          {fileRef.name}
        </span>
        <span className="text-[10px] text-muted-foreground">
          {fileRef.size !== undefined ? formatBytes(fileRef.size) : "workspace"}
        </span>
      </div>
      <button
        type="button"
        onClick={onRemove}
        className="absolute right-0.5 top-0.5 inline-flex h-5 w-5 items-center justify-center rounded text-muted-foreground transition-colors hover:bg-destructive/20 hover:text-destructive"
        aria-label={`Remove ${fileRef.name}`}
      >
        <X className="h-3 w-3" />
      </button>
    </div>
  );
}

function AttachmentChip({
  attachment,
  onRemove,
}: {
  attachment: InputAttachment;
  onRemove: () => void;
}) {
  const isImage = attachment.previewUrl !== null;
  return (
    <div
      className={cn(
        "group/chip relative flex items-center gap-1.5 rounded-md border bg-muted/50 py-1 pl-1.5 pr-6 text-xs text-foreground",
        isImage && "pl-1",
      )}
    >
      {isImage ? (
        <img
          src={attachment.previewUrl ?? ""}
          alt={attachment.name}
          className="h-7 w-7 rounded object-cover"
        />
      ) : (
        <FileIcon className="h-3.5 w-3.5 text-muted-foreground" />
      )}
      <div className="flex flex-col leading-tight">
        <span className="max-w-[200px] truncate font-medium">
          {attachment.name}
        </span>
        <span className="text-[10px] text-muted-foreground">
          {formatBytes(attachment.file.size)}
        </span>
      </div>
      <button
        type="button"
        onClick={onRemove}
        className="absolute right-0.5 top-0.5 inline-flex h-5 w-5 items-center justify-center rounded text-muted-foreground transition-colors hover:bg-destructive/20 hover:text-destructive"
        aria-label={`Remove ${attachment.name}`}
      >
        <X className="h-3 w-3" />
      </button>
    </div>
  );
}

export function formatBytes(n: number): string {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / (1024 * 1024)).toFixed(1)} MB`;
}
