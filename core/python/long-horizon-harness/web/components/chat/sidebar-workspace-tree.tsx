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
  useCallback,
  useEffect,
  useRef,
  useState,
  type MouseEvent,
} from "react";
import {
  ChevronRight,
  Download,
  ExternalLink,
  FileIcon,
  FolderIcon,
  Link as LinkIcon,
  RefreshCw,
  Trash2,
} from "lucide-react";
import { cn } from "@/lib/utils";
import { useViewerOptional } from "./artifact-viewer/viewer-context";
import { FILEREF_DRAG_MIME } from "./workspace-href";
import { formatBytes } from "./input-box";
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from "@/components/ui/alert-dialog";
import { SidebarSearchInput } from "./sidebar-search-input";
import {
  isHiddenName,
  matchesQuery,
  normalizeQuery,
} from "./workspace-tree-filter";

interface TreeEntry {
  name: string;
  kind: "file" | "dir" | "symlink";
  size: number;
  mtime: number;
}

interface DirState {
  loading: boolean;
  error: string | null;
  entries: TreeEntry[] | null;
  truncated: boolean;
}

export interface OptimisticRow {
  name: string;
  size: number;
  // ms timestamp at upload time; converted to unix-seconds on display.
  mtime: number;
}

interface SidebarWorkspaceTreeProps {
  isExpanded: boolean;
  workspaceVersion: number;
  optimisticRows: OptimisticRow[];
  onReconciled: () => void;
  onDelete?: (path: string, opts?: { recursive?: boolean }) => Promise<void>;
  // Hand the parent the refresh action so it can live in the section header
  // (instead of a wasted row here).
  onRegisterRefresh?: (refresh: () => void) => void;
}

const ROOT_PATH = "/workspace";

const buildDownloadHref = (p: string) =>
  `/lha/workspace/download?path=${encodeURIComponent(p)}`;

const buildOpenHref = (p: string) =>
  `/lha/workspace/download?path=${encodeURIComponent(p)}&inline=1`;

function compareDirsFirst(a: TreeEntry, b: TreeEntry): number {
  // Directories first, then files/symlinks. Within each group sort by
  // name case-insensitively so "Alpha" and "alpha" cluster together.
  const aDir = a.kind === "dir" ? 0 : 1;
  const bDir = b.kind === "dir" ? 0 : 1;
  if (aDir !== bDir) return aDir - bDir;
  return a.name.localeCompare(b.name, undefined, { sensitivity: "base" });
}

// A directory is kept while filtering if its own name matches or any fetched
// descendant matches. Recursion is bounded to the in-memory `subDirs` map, so
// collapsed/never-fetched directories are invisible to the filter (documented
// limitation; mitigated by auto-fetching root-level dirs on first filter).
function dirHasMatchInSubtree(
  path: string,
  query: string,
  subDirs: Record<string, DirState>,
): boolean {
  const entries = subDirs[path]?.entries;
  if (!entries) return false;
  for (const entry of entries) {
    if (matchesQuery(entry.name, query)) return true;
    if (
      entry.kind === "dir" &&
      dirHasMatchInSubtree(`${path}/${entry.name}`, query, subDirs)
    ) {
      return true;
    }
  }
  return false;
}

async function fetchDir(
  path: string,
): Promise<{ entries: TreeEntry[]; truncated: boolean }> {
  const url = `/lha/workspace/tree?path=${encodeURIComponent(path)}`;
  const resp = await fetch(url, { cache: "no-store" });
  if (!resp.ok) {
    const detail = await resp.text().catch(() => "");
    throw new Error(
      `${resp.status}${detail ? `: ${detail.slice(0, 200)}` : ""}`,
    );
  }
  return (await resp.json()) as { entries: TreeEntry[]; truncated: boolean };
}

// Persist the show-hidden toggle so dotfiles stay hidden (default) across
// reloads. Mirrors lib/project-collapse.ts's localStorage pattern.
const SHOW_HIDDEN_KEY = "lha:workspace:show-hidden";

function loadShowHidden(): boolean {
  try {
    return localStorage.getItem(SHOW_HIDDEN_KEY) === "1";
  } catch {
    return false;
  }
}

function filterHidden(state: DirState): DirState {
  if (!state.entries) return state;
  return {
    ...state,
    entries: state.entries.filter((e) => !isHiddenName(e.name)),
  };
}

export function SidebarWorkspaceTree({
  isExpanded,
  workspaceVersion,
  optimisticRows,
  onReconciled,
  onDelete,
  onRegisterRefresh,
}: SidebarWorkspaceTreeProps) {
  const [root, setRoot] = useState<DirState>({
    loading: false,
    error: null,
    entries: null,
    truncated: false,
  });
  const [subDirs, setSubDirs] = useState<Record<string, DirState>>({});
  const [openDirs, setOpenDirs] = useState<Set<string>>(new Set());
  const [query, setQuery] = useState("");
  const [showHidden, setShowHidden] = useState(loadShowHidden);
  const isFiltering = normalizeQuery(query) !== "";

  useEffect(() => {
    try {
      localStorage.setItem(SHOW_HIDDEN_KEY, showHidden ? "1" : "0");
    } catch {
      // ignore: storage may be unavailable (private mode)
    }
  }, [showHidden]);

  const loadRoot = useCallback(async () => {
    setRoot((s) => ({ ...s, loading: true, error: null }));
    try {
      const { entries, truncated } = await fetchDir(ROOT_PATH);
      setRoot({ loading: false, error: null, entries, truncated });
      onReconciled();
    } catch (e) {
      setRoot((s) => ({
        ...s,
        loading: false,
        error: e instanceof Error ? e.message : String(e),
      }));
    }
  }, [onReconciled]);

  // Initial load on first expand + refetch when workspaceVersion bumps.
  // Both are gated on isExpanded so a collapsed tree costs zero requests.
  useEffect(() => {
    if (!isExpanded) return;
    void loadRoot();
    // loadRoot identity changes when onReconciled changes; that would
    // cause a duplicate fetch on every parent render. We intentionally
    // depend only on the trigger inputs.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [isExpanded, workspaceVersion]);

  const loadSubDir = useCallback(async (path: string) => {
    setSubDirs((s) => ({
      ...s,
      [path]: {
        loading: true,
        error: null,
        entries: s[path]?.entries ?? null,
        truncated: s[path]?.truncated ?? false,
      },
    }));
    try {
      const { entries, truncated } = await fetchDir(path);
      setSubDirs((s) => ({
        ...s,
        [path]: { loading: false, error: null, entries, truncated },
      }));
    } catch (e) {
      setSubDirs((s) => ({
        ...s,
        [path]: {
          loading: false,
          error: e instanceof Error ? e.message : String(e),
          entries: null,
          truncated: false,
        },
      }));
    }
  }, []);

  // Manual refresh refetches root AND every currently-expanded subdir — root
  // alone leaves the folders the user is actually looking at showing stale
  // contents (e.g. files the agent just wrote into an open subdir).
  const refreshAll = useCallback(() => {
    void loadRoot();
    for (const path of openDirs) void loadSubDir(path);
  }, [loadRoot, loadSubDir, openDirs]);

  useEffect(() => {
    onRegisterRefresh?.(refreshAll);
  }, [onRegisterRefresh, refreshAll]);

  const toggleDir = useCallback(
    (path: string) => {
      setOpenDirs((prev) => {
        const next = new Set(prev);
        if (next.has(path)) {
          next.delete(path);
        } else {
          next.add(path);
          if (!subDirs[path]?.entries) {
            void loadSubDir(path);
          }
        }
        return next;
      });
    },
    [loadSubDir, subDirs],
  );

  // When a filter is first entered, fetch the root-level directories one level
  // deep so their contents become searchable without the user expanding each.
  // A ref tracks which dirs we've auto-fetched so re-renders don't refire.
  const autoFetched = useRef<Set<string>>(new Set());
  useEffect(() => {
    if (!isFiltering) {
      autoFetched.current.clear();
      return;
    }
    for (const entry of root.entries ?? []) {
      if (entry.kind !== "dir") continue;
      const path = `${ROOT_PATH}/${entry.name}`;
      if (autoFetched.current.has(path)) continue;
      if (subDirs[path]?.entries || subDirs[path]?.loading) continue;
      autoFetched.current.add(path);
      void loadSubDir(path);
    }
  }, [isFiltering, root.entries, subDirs, loadSubDir]);

  // While filtering, force-expand every directory that matches or has a fetched
  // matching descendant so the hits are visible. The user's manual `openDirs`
  // is never mutated, so clearing the query restores their expansion exactly.
  // Hidden-file filtering is applied at the data level (once, here) so the
  // recursive render + query-expansion below operate on already-filtered
  // entries without threading the flag through every row component.
  const visibleRoot = showHidden ? root : filterHidden(root);
  const visibleSubDirs = showHidden
    ? subDirs
    : Object.fromEntries(
        Object.entries(subDirs).map(([k, v]) => [k, filterHidden(v)]),
      );

  let effectiveOpenDirs = openDirs;
  if (isFiltering) {
    effectiveOpenDirs = new Set(openDirs);
    const expandMatching = (path: string) => {
      const entries = visibleSubDirs[path]?.entries;
      if (!entries) return;
      for (const entry of entries) {
        if (entry.kind !== "dir") continue;
        const childPath = `${path}/${entry.name}`;
        if (
          matchesQuery(entry.name, query) ||
          dirHasMatchInSubtree(childPath, query, visibleSubDirs)
        ) {
          effectiveOpenDirs.add(childPath);
        }
        expandMatching(childPath);
      }
    };
    for (const entry of visibleRoot.entries ?? []) {
      if (entry.kind !== "dir") continue;
      const path = `${ROOT_PATH}/${entry.name}`;
      if (
        matchesQuery(entry.name, query) ||
        dirHasMatchInSubtree(path, query, visibleSubDirs)
      ) {
        effectiveOpenDirs.add(path);
      }
      expandMatching(path);
    }
  }

  if (!isExpanded) return null;

  return (
    <div className="flex flex-col">
      {!(
        root.entries !== null &&
        root.entries.length === 0 &&
        optimisticRows.length === 0 &&
        !isFiltering
      ) && (
        <div className="px-2 pb-1 pt-0.5">
          <SidebarSearchInput
            value={query}
            onChange={setQuery}
            placeholder="Search files…"
            ariaLabel="Search workspace files"
          />
          <label className="mt-1 flex cursor-pointer select-none items-center gap-1.5 px-1 text-[11px] text-muted-foreground/70 transition-colors hover:text-muted-foreground">
            <input
              type="checkbox"
              className="h-3 w-3 accent-current"
              checked={showHidden}
              onChange={(e) => setShowHidden(e.target.checked)}
            />
            Show hidden files
          </label>
        </div>
      )}
      <RootBody
        root={visibleRoot}
        optimisticRows={optimisticRows}
        query={query}
        isFiltering={isFiltering}
        openDirs={effectiveOpenDirs}
        subDirs={visibleSubDirs}
        onToggle={toggleDir}
        onRetry={loadRoot}
        onDelete={onDelete}
      />
    </div>
  );
}

function RootBody({
  root,
  optimisticRows,
  query,
  isFiltering,
  openDirs,
  subDirs,
  onToggle,
  onRetry,
  onDelete,
}: {
  root: DirState;
  optimisticRows: OptimisticRow[];
  query: string;
  isFiltering: boolean;
  openDirs: Set<string>;
  subDirs: Record<string, DirState>;
  onToggle: (path: string) => void;
  onRetry: () => void;
  onDelete?: (path: string, opts?: { recursive?: boolean }) => Promise<void>;
}) {
  if (root.loading && !root.entries) return <SkeletonRows />;
  if (root.error && !root.entries)
    return <ErrorState message={root.error} onRetry={onRetry} />;

  const serverNames = new Set((root.entries ?? []).map((e) => e.name));
  const visibleOptimistic = optimisticRows
    .filter((o) => !serverNames.has(o.name))
    .map<TreeEntry & { optimistic: true }>((o) => ({
      name: o.name,
      kind: "file",
      size: o.size,
      mtime: Math.floor(o.mtime / 1000),
      optimistic: true,
    }));
  const merged: Array<TreeEntry & { optimistic?: boolean }> = [
    ...(root.entries ?? []),
    ...visibleOptimistic,
  ];
  merged.sort(compareDirsFirst);

  if (!isFiltering && merged.length === 0) {
    return (
      <div className="px-3 py-2 text-[11px] text-muted-foreground">
        Workspace is empty. Drop a file into the Upload section above and
        it&rsquo;ll appear here for the agent to use.
      </div>
    );
  }

  const visible = isFiltering
    ? merged.filter((e) =>
        e.kind === "dir"
          ? matchesQuery(e.name, query) ||
            dirHasMatchInSubtree(`${ROOT_PATH}/${e.name}`, query, subDirs)
          : matchesQuery(e.name, query),
      )
    : merged;

  if (isFiltering && visible.length === 0) {
    const hasCollapsedDirs = merged.some(
      (e) => e.kind === "dir" && !subDirs[`${ROOT_PATH}/${e.name}`]?.entries,
    );
    return (
      <div className="px-3 py-2 text-[11px] text-muted-foreground">
        No files match &ldquo;{query.trim()}&rdquo;.
        {hasCollapsedDirs && (
          <> Deeper folders may not be loaded — expand them to search inside.</>
        )}
      </div>
    );
  }

  return (
    <ul className="flex flex-col">
      {visible.map((e) => (
        <TreeRow
          key={e.name}
          entry={e}
          depth={0}
          parentPath={ROOT_PATH}
          query={query}
          isFiltering={isFiltering}
          openDirs={openDirs}
          subDirs={subDirs}
          onToggle={onToggle}
          onDelete={onDelete}
        />
      ))}
      {root.truncated && (
        <li className="px-3 py-1 text-[10px] italic text-muted-foreground/70">
          … more entries (refresh after the agent prunes)
        </li>
      )}
    </ul>
  );
}

interface TreeRowProps {
  entry: TreeEntry & { optimistic?: boolean };
  depth: number;
  parentPath: string;
  query: string;
  isFiltering: boolean;
  openDirs: Set<string>;
  subDirs: Record<string, DirState>;
  onToggle: (path: string) => void;
  onDelete?: (path: string, opts?: { recursive?: boolean }) => Promise<void>;
}

function TreeRow({
  entry,
  depth,
  parentPath,
  query,
  isFiltering,
  openDirs,
  subDirs,
  onToggle,
  onDelete,
}: TreeRowProps) {
  const path = `${parentPath}/${entry.name}`;
  const padding = { paddingLeft: `${0.5 + depth * 0.75}rem` };
  const [deleteOpen, setDeleteOpen] = useState(false);
  const viewer = useViewerOptional();

  if (entry.kind === "dir") {
    const isOpen = openDirs.has(path);
    const childCount = subDirs[path]?.entries?.length ?? null;
    const deletePrompt =
      childCount === null
        ? `Delete folder "${entry.name}" and all its contents?`
        : childCount === 0
          ? `Delete empty folder "${entry.name}"?`
          : `Delete folder "${entry.name}" and its ${childCount} item${childCount === 1 ? "" : "s"}?`;
    return (
      <li>
        <div
          className="group flex items-center gap-1 rounded px-2 py-0.5 text-xs text-muted-foreground transition-colors hover:bg-accent hover:text-foreground"
          style={padding}
        >
          <button
            type="button"
            onClick={() => onToggle(path)}
            className="flex min-w-0 flex-1 items-center gap-1 text-left"
            aria-expanded={isOpen}
          >
            <ChevronRight
              className={cn(
                "h-3 w-3 shrink-0 transition-transform",
                isOpen && "rotate-90",
              )}
            />
            <FolderIcon className="h-3 w-3 shrink-0" />
            <span className="min-w-0 flex-1 truncate" title={entry.name}>
              {entry.name}
            </span>
          </button>
          <a
            href={buildDownloadHref(path)}
            download={`${entry.name}.zip`}
            onClick={(e) => e.stopPropagation()}
            className="inline-flex h-5 w-5 shrink-0 items-center justify-center rounded text-muted-foreground/60 transition-colors hover:bg-accent hover:text-foreground"
            aria-label={`Download ${entry.name} as zip`}
            title={`Download ${entry.name}.zip`}
          >
            <Download className="h-3 w-3" />
          </a>
          {onDelete && !entry.optimistic && (
            <DeleteEntryDialog
              open={deleteOpen}
              onOpenChange={setDeleteOpen}
              prompt={deletePrompt}
              triggerLabel={`Delete folder ${entry.name}`}
              onConfirm={() => onDelete(path, { recursive: true })}
            />
          )}
        </div>
        {isOpen && (
          <SubDirBody
            path={path}
            depth={depth + 1}
            state={subDirs[path]}
            query={query}
            isFiltering={isFiltering}
            openDirs={openDirs}
            subDirs={subDirs}
            onToggle={onToggle}
            onDelete={onDelete}
          />
        )}
      </li>
    );
  }

  const Icon = entry.kind === "symlink" ? LinkIcon : FileIcon;
  const openable =
    entry.kind === "file" && !entry.optimistic && viewer !== null;
  return (
    <li
      className={cn(
        "flex items-center gap-1.5 px-2 py-0.5 text-xs",
        entry.optimistic
          ? "italic text-muted-foreground/60"
          : "text-muted-foreground",
      )}
      style={padding}
      title={
        entry.optimistic
          ? `${entry.name} (just uploaded — pending refresh)`
          : entry.name
      }
    >
      <Icon className="h-3 w-3 shrink-0" />
      {openable ? (
        <button
          type="button"
          // Dragged into the composer, a row becomes an attachment chip.
          // Deliberately not text/plain: that would make the textarea insert
          // the name as well as showing the chip.
          draggable
          onDragStart={(e) => {
            e.dataTransfer.setData(
              FILEREF_DRAG_MIME,
              JSON.stringify({
                path,
                name: entry.name,
                size: entry.size,
              }),
            );
            e.dataTransfer.effectAllowed = "copy";
          }}
          onClick={() =>
            viewer?.openArtifact({
              name: entry.name,
              // Left blank on purpose: pickRenderer falls back to the
              // filename extension, which is all the tree knows.
              mimeType: "",
              path,
              // Lets image/audio render straight from the endpoint; text
              // kinds are fetched into `bytes` by the viewer.
              url: buildOpenHref(path),
            })
          }
          className="min-w-0 flex-1 truncate text-left hover:text-foreground focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring"
          title={`Open ${entry.name}`}
        >
          {entry.name}
        </button>
      ) : (
        <span className="min-w-0 flex-1 truncate">{entry.name}</span>
      )}
      <span className="shrink-0 text-[10px] text-muted-foreground/60">
        {formatBytes(entry.size)}
      </span>
      {entry.kind === "file" && !entry.optimistic && (
        <a
          href={buildOpenHref(path)}
          target="_blank"
          rel="noopener noreferrer"
          onClick={(e) => e.stopPropagation()}
          className="inline-flex h-5 w-5 shrink-0 items-center justify-center rounded text-muted-foreground/60 transition-colors hover:bg-accent hover:text-foreground"
          aria-label={`Open ${entry.name} in a new tab`}
          title={`Open ${entry.name}`}
        >
          <ExternalLink className="h-3 w-3" />
        </a>
      )}
      {entry.kind === "file" && (
        <a
          href={buildDownloadHref(path)}
          download={entry.name}
          onClick={(e) => e.stopPropagation()}
          className="inline-flex h-5 w-5 shrink-0 items-center justify-center rounded text-muted-foreground/60 transition-colors hover:bg-accent hover:text-foreground"
          aria-label={`Download ${entry.name}`}
          title={`Download ${entry.name}`}
        >
          <Download className="h-3 w-3" />
        </a>
      )}
      {entry.kind === "file" && onDelete && !entry.optimistic && (
        <DeleteEntryDialog
          open={deleteOpen}
          onOpenChange={setDeleteOpen}
          prompt={`Delete "${entry.name}"? This can't be undone.`}
          triggerLabel={`Delete ${entry.name}`}
          onConfirm={() => onDelete(path)}
        />
      )}
    </li>
  );
}

function DeleteEntryDialog({
  open,
  onOpenChange,
  prompt,
  triggerLabel,
  onConfirm,
}: {
  open: boolean;
  onOpenChange: (b: boolean) => void;
  prompt: string;
  triggerLabel: string;
  onConfirm: () => Promise<void> | void;
}) {
  const [busy, setBusy] = useState(false);
  const handleConfirm = async (e: MouseEvent<HTMLButtonElement>) => {
    e.preventDefault();
    setBusy(true);
    try {
      await onConfirm();
      onOpenChange(false);
    } finally {
      setBusy(false);
    }
  };
  return (
    <AlertDialog open={open} onOpenChange={onOpenChange}>
      <button
        type="button"
        onClick={(e) => {
          e.stopPropagation();
          onOpenChange(true);
        }}
        className="inline-flex h-5 w-5 shrink-0 items-center justify-center rounded text-muted-foreground/60 transition-colors hover:bg-destructive/10 hover:text-destructive focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
        aria-label={triggerLabel}
        title={triggerLabel}
      >
        <Trash2 className="h-3 w-3" />
      </button>
      {open && (
        <AlertDialogContent onClick={(e) => e.stopPropagation()}>
          <AlertDialogHeader>
            <AlertDialogTitle>Delete?</AlertDialogTitle>
            <AlertDialogDescription>{prompt}</AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel disabled={busy}>Cancel</AlertDialogCancel>
            <AlertDialogAction
              disabled={busy}
              onClick={handleConfirm}
              className="bg-destructive text-destructive-foreground hover:bg-destructive/90"
            >
              Delete
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      )}
    </AlertDialog>
  );
}

function SubDirBody({
  path,
  depth,
  state,
  query,
  isFiltering,
  openDirs,
  subDirs,
  onToggle,
  onDelete,
}: {
  path: string;
  depth: number;
  state: DirState | undefined;
  query: string;
  isFiltering: boolean;
  openDirs: Set<string>;
  subDirs: Record<string, DirState>;
  onToggle: (p: string) => void;
  onDelete?: (path: string, opts?: { recursive?: boolean }) => Promise<void>;
}) {
  const indent = { paddingLeft: `${0.75 + depth * 0.75}rem` };

  if (!state || (state.loading && !state.entries)) {
    return (
      <div className="py-0.5" style={indent}>
        <SkeletonRows compact />
      </div>
    );
  }
  if (state.error) {
    return (
      <div
        className="px-3 py-1 text-[10px] text-destructive"
        style={indent}
        title={state.error}
      >
        ⚠ {state.error.slice(0, 80)}
      </div>
    );
  }
  if (!state.entries || state.entries.length === 0) {
    return (
      <div
        className="px-3 py-0.5 text-[10px] italic text-muted-foreground/60"
        style={indent}
      >
        empty
      </div>
    );
  }

  const sorted = [...state.entries].sort(compareDirsFirst);
  const visible = isFiltering
    ? sorted.filter((e) =>
        e.kind === "dir"
          ? matchesQuery(e.name, query) ||
            dirHasMatchInSubtree(`${path}/${e.name}`, query, subDirs)
          : matchesQuery(e.name, query),
      )
    : sorted;
  return (
    <ul className="flex flex-col">
      {visible.map((e) => (
        <TreeRow
          key={e.name}
          entry={e}
          depth={depth}
          parentPath={path}
          query={query}
          isFiltering={isFiltering}
          openDirs={openDirs}
          subDirs={subDirs}
          onToggle={onToggle}
          onDelete={onDelete}
        />
      ))}
      {state.truncated && (
        <li
          className="py-0.5 text-[10px] italic text-muted-foreground/70"
          style={indent}
        >
          … more entries
        </li>
      )}
    </ul>
  );
}

function SkeletonRows({ compact = false }: { compact?: boolean }) {
  return (
    <div
      className={cn(
        "flex flex-col gap-0.5",
        compact ? "px-2 py-0.5" : "px-3 py-1",
      )}
    >
      {Array.from({ length: compact ? 2 : 3 }).map((_, i) => (
        <div
          key={i}
          className="h-3.5 motion-safe:animate-pulse rounded bg-muted/40"
        />
      ))}
    </div>
  );
}

function ErrorState({
  message,
  onRetry,
}: {
  message: string;
  onRetry: () => void;
}) {
  return (
    <div className="flex flex-col items-start gap-1 px-3 py-2 text-[11px] text-muted-foreground">
      <span className="text-destructive">Couldn&apos;t load workspace</span>
      <span
        className="truncate text-[10px] text-muted-foreground/60"
        title={message}
      >
        {message}
      </span>
      <button
        type="button"
        onClick={onRetry}
        className="rounded border border-border/70 px-1.5 py-0.5 text-[10px] text-foreground hover:bg-accent"
      >
        Retry
      </button>
    </div>
  );
}
