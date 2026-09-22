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
import {
  Braces,
  Code,
  Download,
  File as FileIcon,
  FileText,
  Globe,
  Image as ImageIcon,
  FileType,
  Lock,
  Music,
  X,
} from "lucide-react";
import { toast } from "sonner";
import { cn } from "@/lib/utils";
import { resolveAnchor } from "@/lib/selection-anchor";
import {
  applyMarkdownCommand,
  type MarkdownCommand,
} from "@/lib/markdown-commands";
import { MarkdownToolbar } from "./markdown-toolbar";
import { useBusy } from "../busy-context";
import { CopyButton } from "../copy-button";
import { Textarea } from "@/components/ui/textarea";
import { useViewer } from "./viewer-context";
import {
  ArtifactBody,
  dataUrl,
  decodeText,
  encodeText,
  isLosslessText,
  isTextKind,
  pickRenderer,
  supportsPreview,
  type RendererKind,
  type ViewMode,
} from "./renderers";

const KIND_ICON: Record<RendererKind, typeof FileText> = {
  markdown: FileText,
  code: Code,
  image: ImageIcon,
  audio: Music,
  html: Globe,
  json: Braces,
  pdf: FileType,
  download: FileIcon,
};

const TEXT_KINDS: ReadonlySet<RendererKind> = new Set([
  "markdown",
  "code",
  "json",
  "html",
]);

// Long enough that a normal typing burst is one request, short enough that
// closing the panel right after a sentence doesn't feel like a gamble.
const AUTOSAVE_DEBOUNCE_MS = 800;

type SaveState = "idle" | "pending" | "saving" | "saved" | "error";

export function ArtifactViewer() {
  const {
    tabs,
    activeId,
    activateTab,
    closeTab,
    reloadTab,
    setBytes,
    setPendingSelection,
  } = useViewer();
  const [mode, setMode] = useState<ViewMode>("preview");
  const [draft, setDraft] = useState<string | null>(null);
  const [saveState, setSaveState] = useState<SaveState>("idle");
  const busy = useBusy();
  // Read from effects that must not re-run per tab-list change.
  const tabsRef = useRef(tabs);
  tabsRef.current = tabs;

  const active = tabs.find((t) => t.id === activeId) ?? null;

  // Tree-opened tabs arrive with a path but no bytes. Refetch on every
  // revision bump too: the agent rewrites files behind our back, so cached
  // bytes are only trustworthy until the next turn ends.
  const activePath = active?.path;
  const revision = active?.revision ?? 0;
  // Set only once a response has actually been applied. Marking it when the
  // request starts strands the tab under StrictMode: the first effect pass is
  // cancelled by its own cleanup, and the second pass sees the key already
  // recorded and returns without refetching.
  const loadedKey = useRef<string | null>(null);
  const fetchKey = activeId && activePath ? `${activeId}:${revision}` : null;
  const hasBytes = active?.bytes != null;
  // Deps are the fetch *target* only. Anything derived from lastFetched would
  // flip false the moment the effect starts, and the next unrelated re-render
  // (a turn ending re-renders this tree constantly) would tear down the
  // in-flight request and drop its response.
  useEffect(() => {
    if (!activeId || !activePath || !fetchKey) return;
    if (loadedKey.current === fetchKey) return;
    const fileName = activePath.split("/").pop() || activePath;
    let cancelled = false;
    // Bytes handed to us at revision 0 are fresh by construction; only a bump
    // (re-open, or a turn ending) means the copy we hold may be behind.
    if (hasBytes && revision === 0) return;
    void (async () => {
      try {
        const r = await fetch(
          `/lha/workspace/download?path=${encodeURIComponent(activePath)}`,
          { cache: "no-store" },
        );
        // The file is gone — renamed or deleted out from under us. Keeping
        // the tab would show contents that no longer exist anywhere.
        if (r.status === 404) {
          if (!cancelled) closeTab(activeId);
          return;
        }
        if (!r.ok) {
          // Silence here meant the panel kept showing bytes that no longer
          // matched disk, and the next keystroke saved them back over
          // whatever had replaced them.
          if (!cancelled) {
            toast.error(`Couldn't refresh ${fileName}`, {
              description: `The panel may be out of date (${r.status}).`,
            });
          }
          return;
        }
        const buf = new Uint8Array(await r.arrayBuffer());
        let bin = "";
        for (const b of buf) bin += String.fromCharCode(b);
        if (cancelled) return;
        loadedKey.current = fetchKey;
        setBytes(activeId, btoa(bin));
      } catch (e) {
        if (!cancelled) {
          toast.error(`Couldn't refresh ${fileName}`, {
            description: e instanceof Error ? e.message : String(e),
          });
        }
      }
    })();
    return () => {
      cancelled = true;
    };
    // hasBytes/revision are read-through guards for the first run of a given
    // fetchKey, which already encodes the revision.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeId, activePath, fetchKey, setBytes, closeTab]);

  // Autosave is fire-and-forget, so it can't close over render state: a tab
  // switch or unmount has to be able to flush the last keystrokes after the
  // component has already moved on. Everything it needs lives in this ref.
  const pendingRef = useRef<{
    id: string;
    path: string;
    name: string;
    content: string;
  } | null>(null);

  const flush = useCallback(async () => {
    const p = pendingRef.current;
    if (!p) return;
    pendingRef.current = null;
    setSaveState("saving");
    try {
      const r = await fetch(
        `/lha/workspace/file?path=${encodeURIComponent(p.path)}`,
        {
          method: "PUT",
          headers: { "content-type": "application/json" },
          body: JSON.stringify({ content: p.content }),
        },
      );
      if (!r.ok) {
        const detail = await r.text().catch(() => "");
        // Put it back so the next debounce or tab switch retries it — but
        // only if nothing newer arrived while the request was in flight,
        // or this would resurrect stale text over the user's latest edit.
        pendingRef.current ??= p;
        setSaveState("error");
        toast.error(`Couldn't save ${p.name} (${r.status})`, {
          description: detail.slice(0, 200) || undefined,
        });
        return;
      }
      setBytes(p.id, encodeText(p.content));
      // Drop the draft once it matches disk, so a later reload (the agent
      // editing the same file) isn't masked by a stale local copy. Skipped
      // if the user kept typing during the request — that draft is newer.
      setDraft((d) => (d === p.content ? null : d));
      setSaveState("saved");
    } catch (e) {
      pendingRef.current ??= p;
      setSaveState("error");
      toast.error(`Couldn't save ${p.name}`, {
        description: e instanceof Error ? e.message : String(e),
      });
    }
  }, [setBytes]);

  // Debounced autosave. The cleanup cancels the timer on every keystroke and
  // flushes on tab switch/unmount, so the trailing edit is never dropped.
  useEffect(() => {
    if (!pendingRef.current) return;
    const t = setTimeout(() => void flush(), AUTOSAVE_DEBOUNCE_MS);
    return () => {
      clearTimeout(t);
    };
  }, [draft, flush]);

  // A freshly-activated tab opens in its rendered view. Flush first: the
  // draft we're about to drop belongs to the tab we're leaving.
  useEffect(() => {
    return () => {
      void flush();
    };
  }, [activeId, flush]);

  // A turn starting beats the debounce to the punch — otherwise the pending
  // write could land after the agent's own and clobber it.
  useEffect(() => {
    if (busy) void flush();
  }, [busy, flush]);

  // A turn ending is the one signal that the agent may have rewritten the
  // open file. There's no file-changed event to subscribe to, so re-read it.
  const wasBusy = useRef(busy);
  useEffect(() => {
    const ended = wasBusy.current && !busy;
    wasBusy.current = busy;
    if (!ended) return;
    for (const t of tabsRef.current) {
      if (t.path) reloadTab(t.id);
    }
  }, [busy, reloadTab]);

  useEffect(() => {
    setMode("preview");
    setDraft(null);
    setSaveState("idle");
  }, [activeId]);

  // Leaving the panel must not strand a chip pointing at a file you can no
  // longer see.
  useEffect(() => {
    return () => setPendingSelection(null);
  }, [setPendingSelection]);

  // The native highlight vanishes when focus moves to the composer, so the
  // captured anchor — surfaced as a chip — becomes the only remaining cue.
  const handleSelect = (el: HTMLTextAreaElement) => {
    if (!active?.path) return;
    const { selectionStart, selectionEnd, value } = el;
    if (selectionEnd <= selectionStart) {
      setPendingSelection(null);
      return;
    }
    const r = resolveAnchor(value, selectionStart, selectionEnd);
    setPendingSelection(r.ok ? { path: active.path, anchor: r.anchor } : null);
  };

  // The command needs the live caret, which lives on the DOM node rather than
  // in React state.
  const editorRef = useRef<HTMLTextAreaElement | null>(null);

  const runCommand = (cmd: MarkdownCommand) => {
    const el = editorRef.current;
    if (!el || locked) return;
    const next = applyMarkdownCommand(
      { text: el.value, start: el.selectionStart, end: el.selectionEnd },
      cmd,
    );
    setPendingSelection(null);
    handleChange(next.text);
    // React re-renders from `draft`, so the caret has to be restored after
    // the new value lands or it snaps to the end of the document.
    requestAnimationFrame(() => {
      el.focus();
      el.setSelectionRange(next.start, next.end);
    });
  };

  const handleChange = (value: string) => {
    if (!active?.path) return;
    setDraft(value);
    pendingRef.current = {
      id: active.id,
      path: active.path,
      name: active.name,
      content: value,
    };
    setSaveState("pending");
  };

  if (!active) {
    return (
      <div className="flex h-full items-center justify-center px-6 text-center text-xs text-muted-foreground">
        No file open. Open a file from the chat to view it here.
      </div>
    );
  }

  const kind = pickRenderer(active.mimeType, active.name);
  const canPreview = supportsPreview(kind);
  const copyable = TEXT_KINDS.has(kind);
  // Only live workspace files are editable — artifact snapshots are frozen
  // copies with nowhere to write back to. Source view is the editor for
  // those, so there's no separate "edit" mode to toggle.
  // Lossless, not merely decodable: saving a U+FFFD-mangled decode of a
  // non-UTF-8 file would destroy the bytes it could not represent.
  const editable =
    Boolean(active.path) && isTextKind(kind) && isLosslessText(active);
  const editing = mode === "raw" && editable;
  // Autosave means an in-flight turn can't be allowed to overlap typing:
  // the agent may rewrite the file under us and our next debounce would
  // clobber it. Lock the textarea instead, and show why.
  const locked = editing && busy;
  // "source" is only worth a toggle when there's a rendered view to switch
  // away from, or when it doubles as the editor.
  const showModes = canPreview || editable;
  const saveLabel: Record<SaveState, string | null> = {
    idle: null,
    pending: "Unsaved…",
    saving: "Saving…",
    saved: "Saved",
    error: "Save failed",
  };

  return (
    <div className="flex h-full min-h-0 flex-col">
      <div
        role="tablist"
        aria-label="Open files"
        className="flex shrink-0 items-stretch gap-0.5 overflow-x-auto border-b border-border/60 px-1"
      >
        {tabs.map((t) => {
          const Icon = KIND_ICON[pickRenderer(t.mimeType, t.name)];
          const selected = t.id === activeId;
          return (
            <div
              key={t.id}
              className={cn(
                "group/tab flex items-center gap-1.5 border-b-2 py-1.5 pl-2 pr-1 text-xs transition-colors",
                selected
                  ? "border-primary text-foreground"
                  : "border-transparent text-muted-foreground hover:text-foreground",
              )}
            >
              <button
                type="button"
                role="tab"
                aria-selected={selected}
                onClick={() => activateTab(t.id)}
                className="flex max-w-[16ch] items-center gap-1.5 truncate focus-visible:outline-none"
                title={t.name}
              >
                <Icon className="h-3.5 w-3.5 shrink-0" />
                <span className="truncate">{t.name}</span>
              </button>
              <button
                type="button"
                aria-label={`Close ${t.name}`}
                onClick={() => closeTab(t.id)}
                className="rounded p-0.5 text-muted-foreground/60 opacity-0 transition-opacity hover:bg-muted hover:text-foreground group-hover/tab:opacity-100 focus-visible:opacity-100"
              >
                <X className="h-3 w-3" />
              </button>
            </div>
          );
        })}
      </div>

      <div className="flex shrink-0 items-center justify-between gap-2 border-b border-border/60 px-3 py-1.5">
        <span className="min-w-0 truncate font-mono text-[11px] text-muted-foreground">
          {active.name}
        </span>
        <div className="flex shrink-0 items-center gap-1">
          {showModes && (
            <div className="flex items-center rounded-md border border-border/60 p-0.5 text-[11px]">
              {(["preview", "raw"] as const).map((m) => (
                <button
                  key={m}
                  type="button"
                  onClick={() => {
                    // Not setDraft(null): the pending write would still land
                    // 800ms later while both views showed the pre-edit bytes.
                    void flush();
                    setMode(m);
                  }}
                  title={
                    m === "raw" && editable && busy
                      ? "The agent is working — editing resumes when the turn ends"
                      : undefined
                  }
                  className={cn(
                    "rounded px-2 py-0.5 transition-colors",
                    mode === m
                      ? "bg-muted text-foreground"
                      : "text-muted-foreground hover:text-foreground",
                  )}
                >
                  {m === "raw" ? (editable ? "Source" : "Raw") : "Preview"}
                </button>
              ))}
            </div>
          )}
          {locked && (
            <span
              className="inline-flex items-center gap-1 text-[11px] text-muted-foreground"
              title="The agent may be rewriting this file — editing resumes when the turn ends"
            >
              <Lock className="h-3 w-3" />
              Agent working
            </span>
          )}
          {editing && !locked && saveLabel[saveState] && (
            <span
              className={cn(
                "text-[11px]",
                saveState === "error"
                  ? "text-destructive"
                  : "text-muted-foreground",
              )}
              aria-live="polite"
            >
              {saveLabel[saveState]}
            </span>
          )}
          {copyable && (
            // The draft is what the user is looking at; copying the saved
            // bytes would silently hand back the pre-edit text.
            <CopyButton getText={() => draft ?? decodeText(active) ?? ""} />
          )}
          {(() => {
            const href = dataUrl(active);
            return (
              href && (
                <a
                  href={href}
                  download={active.name}
                  aria-label="Download"
                  className="inline-flex h-7 w-7 items-center justify-center rounded-md border bg-card text-muted-foreground shadow-sm transition-colors hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring max-md:h-11 max-md:w-11"
                >
                  <Download className="h-3 w-3" />
                </a>
              )
            );
          })()}
        </div>
      </div>

      {/* Markdown only: bold and bullets are meaningless in a .py file. */}
      {editing && kind === "markdown" && (
        <MarkdownToolbar onCommand={runCommand} disabled={locked} />
      )}

      <div className="min-h-0 flex-1 overflow-auto">
        {editing ? (
          <Textarea
            ref={editorRef}
            value={draft ?? decodeText(active) ?? ""}
            onChange={(e) => {
              // Offsets are meaningless the moment the text shifts.
              setPendingSelection(null);
              handleChange(e.target.value);
            }}
            onSelect={(e) => handleSelect(e.currentTarget)}
            readOnly={locked}
            spellCheck={false}
            className={cn(
              "h-full min-h-full resize-none rounded-none border-0 bg-transparent p-3 font-mono text-xs focus-visible:ring-0",
              locked && "cursor-not-allowed opacity-60",
            )}
          />
        ) : (
          <ArtifactBody
            tab={active}
            kind={kind}
            mode={canPreview || editable ? mode : "preview"}
          />
        )}
      </div>
    </div>
  );
}
