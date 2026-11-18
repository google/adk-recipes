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

import { Archive, Ellipsis, Menu, Pencil, Trash2 } from "lucide-react";
import {
  type ReactNode,
  useCallback,
  useEffect,
  useRef,
  useState,
} from "react";
import { Button, IconButton, InlineConfirm, TextInput } from "../common/ui";

export interface ProjectMenuActions {
  name: string;
  hideLabel: string;
  onRename: (name: string) => void;
  onHide: () => void;
}

function useDismiss(open: boolean, close: () => void) {
  const root = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!open) return;
    const onEvent = (e: Event) => {
      if (
        e instanceof KeyboardEvent
          ? e.key === "Escape"
          : !root.current?.contains(e.target as Node)
      )
        close();
    };
    document.addEventListener("pointerdown", onEvent);
    document.addEventListener("keydown", onEvent);
    return () => {
      document.removeEventListener("pointerdown", onEvent);
      document.removeEventListener("keydown", onEvent);
    };
  }, [open, close]);
  return root;
}

function HeaderMenu({
  title,
  children,
}: {
  title: string;
  children: (close: () => void) => ReactNode;
}) {
  const [open, setOpen] = useState(false);
  const close = useCallback(() => setOpen(false), []);
  const root = useDismiss(open, close);
  return (
    <div className="thread-menu" ref={root}>
      <IconButton
        id="thread-menu-btn"
        className="top-chrome-btn"
        label={`More for ${title}`}
        aria-expanded={open}
        aria-haspopup="menu"
        onClick={() => setOpen((o) => !o)}
      >
        <Ellipsis size={18} />
      </IconButton>
      {open && (
        <div className="thread-menu-panel" role="menu">
          {children(close)}
        </div>
      )}
    </div>
  );
}

function ThreadMenuItems({
  onDelete,
  close,
}: {
  onDelete: () => void;
  close: () => void;
}) {
  return (
    <InlineConfirm
      question="Delete this thread's conversation? Files stay."
      onConfirm={() => {
        close();
        onDelete();
      }}
      trigger={(ask) => (
        <Button
          variant="quiet"
          role="menuitem"
          className="thread-menu-item"
          data-tone="danger"
          icon={<Trash2 size={16} aria-hidden />}
          iconFirst
          onClick={ask}
        >
          Delete thread
        </Button>
      )}
    />
  );
}

function ProjectMenuItems({
  actions,
  close,
}: {
  actions: ProjectMenuActions;
  close: () => void;
}) {
  const [renaming, setRenaming] = useState(false);
  const [name, setName] = useState(actions.name);
  if (renaming) {
    const save = () => {
      const trimmed = name.trim();
      if (trimmed && trimmed !== actions.name) actions.onRename(trimmed);
      close();
    };
    return (
      <form
        className="thread-menu-rename"
        onSubmit={(e) => {
          e.preventDefault();
          save();
        }}
      >
        <TextInput
          autoFocus
          aria-label="Project name"
          value={name}
          maxLength={60}
          onChange={(e) => setName(e.target.value)}
        />
        <span className="ui-confirm-actions">
          <Button size="sm" variant="secondary" onClick={close}>
            Cancel
          </Button>
          <Button size="sm" variant="primary" type="submit">
            Save
          </Button>
        </span>
      </form>
    );
  }
  return (
    <>
      <Button
        variant="quiet"
        role="menuitem"
        className="thread-menu-item"
        icon={<Pencil size={16} aria-hidden />}
        iconFirst
        onClick={() => setRenaming(true)}
      >
        Rename
      </Button>
      <Button
        variant="quiet"
        role="menuitem"
        className="thread-menu-item"
        icon={<Archive size={16} aria-hidden />}
        iconFirst
        onClick={() => {
          close();
          actions.onHide();
        }}
      >
        {actions.hideLabel}
      </Button>
    </>
  );
}

export function StageHeader({
  thread,
  attention,
  onOpenMenu,
}: {
  thread?: {
    title: string;
    statusLine: string;
    onHome: () => void;
    onDelete?: () => void;
    project?: ProjectMenuActions;
  };
  attention: boolean;
  onOpenMenu: () => void;
}) {
  return (
    <header className="stage-top-bar">
      <IconButton
        id="open-nav-drawer-btn"
        className="top-chrome-btn"
        label={attention ? "Open menu, something needs you" : "Open menu"}
        onClick={onOpenMenu}
      >
        <Menu size={18} />
        {attention && <span className="top-menu-dot" aria-hidden />}
      </IconButton>
      {thread ? (
        <>
          <button
            type="button"
            id="go-home-btn"
            className="thread-head"
            aria-label={`${thread.title}. ${
              thread.statusLine && thread.onDelete
                ? `Back to ${thread.statusLine}`
                : "Go home"
            }`}
            onClick={thread.onHome}
          >
            <span className="thread-title">{thread.title}</span>
            {thread.statusLine && (
              <p className="thread-status">{thread.statusLine}</p>
            )}
          </button>
          {thread.onDelete && (
            <HeaderMenu title={thread.title}>
              {(close) =>
                thread.onDelete ? (
                  <ThreadMenuItems onDelete={thread.onDelete} close={close} />
                ) : null
              }
            </HeaderMenu>
          )}
          {thread.project && (
            <HeaderMenu title={thread.title}>
              {(close) =>
                thread.project ? (
                  <ProjectMenuItems actions={thread.project} close={close} />
                ) : null
              }
            </HeaderMenu>
          )}
        </>
      ) : null}
    </header>
  );
}
