/**
 * Copyright 2026 Google LLC
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     https://www.apache.org/licenses/LICENSE-2.0
 *
 * Unless required by applicable law or agreed to in writing, software
 * distributed under the License is distributed on an "AS IS" BASIS,
 * WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 * See the License for the specific language governing permissions and
 * limitations under the License.
 */

import {
  useEffect,
  useRef,
  useState,
  type FormEvent,
  type KeyboardEvent,
} from "react";
import {
  CloseIcon,
  ImageIcon,
  PlusIcon,
  SendIcon,
  SparkleIcon,
  SpinIcon,
  TagIcon,
} from "../../components/icons";
import Markdown from "./Markdown";
import type { ChatEntry } from "./store";

export const SUGGESTIONS = [
  "Do a VTO of Emma with a black leather jacket",
  "Find a red summer dress and try it on a model",
  "Make a 360 spin of a sneaker",
];
const SUGGESTION_ICONS = [ImageIcon, TagIcon, SpinIcon];

/** The agent's mark, next to its messages and in the header. */
const AgentAvatar = ({ size = "w-7 h-7" }: { size?: string }) => (
  <span
    aria-hidden="true"
    className={`${size} shrink-0 rounded-full bg-primary-gradient text-gm-bg flex items-center justify-center shadow-accent`}
  >
    <SparkleIcon size={14} />
  </span>
);

function Bubble({ entry }: { entry: ChatEntry }) {
  const user = entry.role === "user";
  const tone = user
    ? "bg-gm-accent text-[#0B1220] rounded-br-md"
    : entry.error
      ? "bg-gm-error/10 border border-gm-error/30 text-gm-error rounded-bl-md"
      : "bg-white/[0.06] border border-white/[0.08] text-gm-text-primary rounded-bl-md";
  return (
    <li
      role={entry.error ? "alert" : undefined}
      className={`flex items-end gap-2 animate-fade-in-up ${user ? "self-end" : "self-start"} max-w-[88%]`}
    >
      {!user && <AgentAvatar />}
      <div
        className={`min-w-0 px-3.5 py-2.5 rounded-2xl text-sm leading-relaxed break-words ${tone}`}
      >
        {user || entry.error ? (
          <span className="whitespace-pre-wrap">{entry.text}</span>
        ) : (
          <Markdown text={entry.text} />
        )}
      </div>
    </li>
  );
}

type ChatPanelProps = {
  messages: ChatEntry[];
  /** The saved conversation was read: until then nothing is sent and no suggestion shows. */
  loaded: boolean;
  busy: boolean;
  onSend: (text: string) => void;
  onNew: () => void;
  /** Phones: the panel is a drawer, open or closed. */
  open: boolean;
  onClose: () => void;
};

/** The conversation with the Studio agent: it prefills the studio and runs generations. */
export default function ChatPanel({
  messages,
  loaded,
  busy,
  onSend,
  onNew,
  open,
  onClose,
}: ChatPanelProps) {
  const [draft, setDraft] = useState("");
  const list = useRef<HTMLOListElement>(null);
  const input = useRef<HTMLTextAreaElement>(null);
  const wasBusy = useRef(busy);

  // biome-ignore lint/correctness/useExhaustiveDependencies: scrolls to the end when any of them changes
  useEffect(() => {
    const node = list.current;
    if (node) node.scrollTop = node.scrollHeight;
  }, [messages, busy, open]);
  // The input is off during a turn; typing resumes there once the agent replied.
  useEffect(() => {
    if (wasBusy.current && !busy) input.current?.focus();
    wasBusy.current = busy;
  }, [busy]);

  const send = (text: string) => {
    if (!text.trim() || busy || !loaded) return;
    onSend(text);
    setDraft("");
  };
  const onSubmit = (event: FormEvent) => {
    event.preventDefault();
    send(draft);
  };
  const onKeyDown = (event: KeyboardEvent<HTMLTextAreaElement>) => {
    if (
      event.key === "Enter" &&
      !event.shiftKey &&
      !event.nativeEvent.isComposing
    ) {
      event.preventDefault();
      send(draft);
    }
  };

  return (
    <aside
      aria-label="Studio agent"
      className={`glass-panel flex-col overflow-hidden ${open ? "flex fixed inset-0 z-[60] !bg-gm-raised animate-slide-up" : "hidden"} lg:flex lg:sticky lg:inset-auto lg:top-[84px] lg:z-auto lg:h-[calc(100dvh-13rem)] lg:min-h-[480px] lg:max-h-[880px] lg:rounded-2xl lg:animate-none`}
    >
      <header className="flex items-center gap-3 px-4 py-3 border-b border-white/[0.07] bg-white/[0.02]">
        <AgentAvatar size="w-8 h-8" />
        <div className="flex-1 min-w-0">
          <h2 className="text-h3">Studio agent</h2>
          <p className="flex items-center gap-1.5 text-[11px] text-gm-text-tertiary min-w-0">
            <span
              aria-hidden="true"
              className={`shrink-0 w-1.5 h-1.5 rounded-full ${busy ? "bg-gm-accent animate-pulse-soft" : "bg-gm-success"}`}
            />
            <span className="truncate">
              {busy
                ? "Working on it"
                : "Sets up the studio and runs generations"}
            </span>
          </p>
        </div>
        <button
          type="button"
          onClick={onNew}
          title="Start a new studio and conversation"
          className="glass-button rounded-lg h-8 px-2.5 text-xs font-medium flex items-center gap-1"
        >
          <PlusIcon size={13} />
          New
        </button>
        <button
          type="button"
          onClick={onClose}
          aria-label="Close the chat"
          className="lg:hidden w-8 h-8 rounded-lg hover:bg-white/10 flex items-center justify-center"
        >
          <CloseIcon size={16} />
        </button>
      </header>

      <ol
        ref={list}
        aria-live="polite"
        className="flex-1 overflow-y-auto px-4 py-5 flex flex-col gap-4"
      >
        {loaded && !messages.length && (
          <li className="flex flex-col gap-4 my-auto">
            <div className="flex flex-col items-center text-center gap-3 px-2">
              <AgentAvatar size="w-11 h-11" />
              <p className="text-h3">What shall we make?</p>
              <p className="text-sm text-gm-text-secondary">
                Ask for a try-on, a video or a 360° spin. The agent fills in the
                studio and runs the generation; your own changes are shared with
                it.
              </p>
            </div>
            <div className="flex flex-col gap-2">
              {SUGGESTIONS.map((text, i) => {
                const Icon = SUGGESTION_ICONS[i];
                return (
                  <button
                    key={text}
                    type="button"
                    onClick={() => send(text)}
                    disabled={busy}
                    className="glass-button rounded-xl px-3 py-2.5 text-sm text-left text-gm-text-secondary hover:text-gm-text-primary flex items-center gap-2.5"
                  >
                    <Icon size={15} className="shrink-0 text-gm-accent" />
                    {text}
                  </button>
                );
              })}
            </div>
          </li>
        )}
        {messages.map((entry, i) => (
          // biome-ignore lint/suspicious/noArrayIndexKey: messages have no ids; the list only grows at the end
          <Bubble key={i} entry={entry} />
        ))}
        {busy && (
          <li role="status" className="self-start flex items-end gap-2">
            <AgentAvatar />
            <span className="flex items-center gap-2 px-3.5 py-3 rounded-2xl rounded-bl-md bg-white/[0.06] border border-white/[0.08] text-sm text-gm-text-secondary">
              <span aria-hidden="true" className="flex gap-1">
                <span className="typing-dot" />
                <span className="typing-dot" />
                <span className="typing-dot" />
              </span>
              Working…
            </span>
          </li>
        )}
      </ol>

      <form onSubmit={onSubmit} className="p-3 border-t border-white/[0.07]">
        <div className="flex items-end gap-2 rounded-2xl bg-black/30 border border-white/10 p-1.5 pl-3.5 transition-all focus-within:border-gm-accent/60 focus-within:shadow-focus">
          <textarea
            ref={input}
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            onKeyDown={onKeyDown}
            rows={2}
            disabled={busy || !loaded}
            aria-label="Message the studio agent"
            placeholder="Ask the studio agent…"
            className="flex-1 bg-transparent py-1.5 text-sm text-gm-text-primary placeholder:text-gm-text-tertiary resize-none outline-none focus-visible:outline-none disabled:opacity-60"
          />
          <button
            type="submit"
            disabled={busy || !loaded || !draft.trim()}
            aria-label="Send"
            title="Send (Enter)"
            className="btn-primary !p-0 w-9 h-9 !rounded-xl flex items-center justify-center shrink-0"
          >
            <SendIcon size={16} />
          </button>
        </div>
        <p className="hidden lg:block mt-1.5 px-1 text-[11px] text-gm-text-tertiary">
          Enter to send · Shift+Enter for a new line
        </p>
      </form>
    </aside>
  );
}
