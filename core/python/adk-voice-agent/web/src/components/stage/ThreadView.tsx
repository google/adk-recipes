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

import { ChevronRight } from "lucide-react";
import { useEffect, useLayoutEffect, useRef, useState } from "react";
import {
  type ThreadDetail,
  type ThreadSummary,
  type ThreadTurn,
  useProjectThread,
  useThreadDetail,
} from "../../lib/api";
import { formatElapsed, plural, timeOfDay } from "../../lib/format";
import { liveProjectThreads } from "../../lib/projects";
import {
  showsAnswer,
  threadTitle,
  threadWhen,
  turnElapsed,
  turnSteps,
} from "../../lib/threads";
import { AdkBrandEmblem } from "../BrandIcons";
import { Markdown } from "../common/Markdown";
import { Button, EmptyState, ErrorNote } from "../common/ui";
import { ThreadMeta } from "./thread/ThreadMeta";
import { ThreadCards, ThreadOutputs } from "./thread/ThreadOutputs";
import {
  type ThreadActions,
  ThreadReply,
  TurnError,
} from "./thread/ThreadReply";
import { useNow } from "./time";

function fallbackTurn(summary: ThreadSummary): ThreadTurn {
  return {
    seq: 1,
    prompt: summary.goal,
    outcome:
      summary.status === "running"
        ? "running"
        : summary.outcome === "failed"
          ? "failed"
          : summary.outcome === "cancelled"
            ? "cancelled"
            : "finished",
    answer: summary.result_text ?? "",
    summary: summary.summary ?? "",
    error: summary.error,
    cards: summary.cards ?? [],
    links: summary.links ?? [],
    started_at: summary.created_at,
    ended_at: summary.ended_at,
    elapsed_seconds: summary.elapsed_seconds,
    events: [],
  };
}

/** "Worked 4m · 3 steps": the plain-language steps a turn took, folded until tapped. */
function WorkedStepsPill({ turn, now }: { turn: ThreadTurn; now: number }) {
  const [open, setOpen] = useState(false);
  const steps = turnSteps(turn.events ?? []);
  if (steps.length === 0) return null;

  const duration = formatElapsed(turnElapsed(turn, now));
  const verb = turn.outcome === "running" ? "Working" : "Worked";

  return (
    <div className="turn-steps" data-open={open || undefined}>
      <button
        type="button"
        className="turn-steps-pill"
        aria-expanded={open}
        onClick={() => setOpen((o) => !o)}
      >
        <ChevronRight size={13} aria-hidden />
        <span>
          {verb} {duration} · {plural(steps.length, "step")}
        </span>
      </button>
      {open && (
        <div className="turn-steps-body">
          <ol className="turn-timeline">
            {steps.map((ev) => (
              <li
                key={ev.seq}
                className="turn-timeline-row"
                data-kind={ev.kind}
              >
                <span className="turn-timeline-dot" aria-hidden />
                <span className="turn-timeline-msg">{ev.message}</span>
                <time className="turn-timeline-time" title={timeOfDay(ev.ts)}>
                  {formatElapsed(Math.max(0, ev.ts - turn.started_at))}
                </time>
              </li>
            ))}
          </ol>
        </div>
      )}
    </div>
  );
}

function TurnItem({ turn, now }: { turn: ThreadTurn; now: number }) {
  const links = turn.links ?? [];

  return (
    <li className="chat-turn">
      {turn.prompt && (
        <div className="chat-user-row">
          <div className="chat-user-bubble">{turn.prompt}</div>
        </div>
      )}

      <div className="chat-agent-row">
        <span className="chat-agent-mark" aria-hidden>
          <AdkBrandEmblem size={18} />
        </span>
        <div className="chat-agent-body">
          <WorkedStepsPill turn={turn} now={now} />
          <ThreadCards cards={turn.cards} endedAt={turn.ended_at} />
          <TurnError error={turn.error} />
          {/* A turn that failed after it answered keeps its answer. */}
          {turn.answer ? (
            <>
              {turn.summary && (
                <p className="chat-turn-summary">{turn.summary}</p>
              )}
              {showsAnswer(turn) && <TurnReport answer={turn.answer} />}
            </>
          ) : !turn.error && turn.outcome === "cancelled" ? (
            <p className="chat-turn-muted">Stopped.</p>
          ) : null}

          <ThreadOutputs links={links} />
        </div>
      </div>
    </li>
  );
}

function TurnReport({ answer }: { answer: string }) {
  const body = useRef<HTMLDivElement>(null);
  const [open, setOpen] = useState(false);
  const [overflows, setOverflows] = useState(false);
  useLayoutEffect(() => {
    const el = body.current;
    if (el) {
      setOverflows(Boolean(answer) && el.scrollHeight > el.clientHeight + 4);
    }
  }, [answer]);
  return (
    <div
      className="chat-turn-answer turn-report"
      data-open={open ? true : undefined}
      data-clamped={!open && overflows ? true : undefined}
    >
      <div ref={body} className="turn-report-body">
        <Markdown source={answer} />
      </div>
      {overflows && (
        <Button
          size="sm"
          variant="quiet"
          className="turn-report-toggle"
          aria-expanded={open}
          onClick={() => setOpen((o) => !o)}
        >
          {open ? "Show less" : "Show full answer"}
        </Button>
      )}
    </div>
  );
}

function FeedItem({
  thread,
  now,
  actions,
  onOpen,
}: {
  thread: ThreadSummary;
  now: number;
  actions: ThreadActions;
  onOpen: () => void;
}) {
  const title = threadTitle(thread);
  const when = threadWhen(thread, now);

  return (
    <li
      className="chat-turn feed-item"
      data-testid={`feed-item-${thread.thread_id}`}
    >
      <div className="chat-user-row">
        <div className="chat-user-bubble">{thread.goal}</div>
      </div>
      <div className="chat-agent-row">
        <span className="chat-agent-mark" aria-hidden>
          <AdkBrandEmblem size={18} />
        </span>
        <div className="chat-agent-body">
          <ThreadReply thread={thread} elapsed={when} actions={actions} />
          {thread.status === "idle" && (
            <ThreadCards cards={thread.cards} endedAt={thread.ended_at} />
          )}
          <ThreadOutputs links={thread.links ?? []} />
          <button
            type="button"
            className="feed-thread-chip"
            aria-label={`Open thread ${title}`}
            onClick={onOpen}
          >
            <span className="feed-thread-title">{title}</span>
            <ThreadMeta thread={thread} when={when} />
            <ChevronRight size={14} aria-hidden />
          </button>
        </div>
      </div>
    </li>
  );
}

function ProjectFeed({
  project,
  threads,
  actions,
  onSelectThread,
}: {
  project: string;
  threads: ThreadSummary[];
  actions: ThreadActions;
  onSelectThread: (thread: ThreadSummary) => void;
}) {
  const now = useNow();
  const projectQuery = useProjectThread(project);
  const list = liveProjectThreads(
    projectQuery.data?.threads ?? [],
    project,
    threads,
  ).sort((a, b) => a.created_at - b.created_at);

  const endRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (list.length > 0) endRef.current?.scrollIntoView({ block: "end" });
  }, [list.length]);

  if (list.length === 0) {
    if (projectQuery.isError) {
      return (
        <ErrorNote>
          Couldn't load this project. Try again in a moment.
        </ErrorNote>
      );
    }
    return projectQuery.isPending ? null : (
      <EmptyState title="Nothing here yet">
        Say what you'd like to do in this project.
      </EmptyState>
    );
  }

  return (
    <div className="project-feed" data-testid="project-feed">
      <ol className="chat-turns">
        {list.map((thread) => (
          <FeedItem
            key={thread.thread_id}
            thread={thread}
            now={now}
            actions={actions}
            onOpen={() => onSelectThread(thread)}
          />
        ))}
      </ol>
      <div ref={endRef} />
    </div>
  );
}

/** Renders a multi-turn thread chat when `threadId` is set, else the project's feed. */
export function ThreadView({
  project,
  threadId,
  threads,
  actions,
  onSelectThread,
}: {
  project?: string;
  threadId?: string;
  threads: ThreadSummary[];
  actions: ThreadActions;
  onSelectThread: (thread: ThreadSummary) => void;
}) {
  const now = useNow();
  const summary = threads.find((t) => t.thread_id === threadId);
  const detailQuery = useThreadDetail(
    threadId,
    summary?.last_seq ?? 0,
    Boolean(threadId) && summary !== undefined && summary.status !== "idle",
  );
  const detail: ThreadDetail | undefined = detailQuery.data;
  const active = detail ?? summary;

  const endRef = useRef<HTMLDivElement>(null);
  const turnCount = detail?.turns?.length ?? (summary ? 1 : 0);
  useEffect(() => {
    if (!threadId || turnCount === 0) return;
    endRef.current?.scrollIntoView({ block: "end" });
  }, [threadId, turnCount]);

  if (!threadId) {
    return (
      <ProjectFeed
        project={project ?? ""}
        threads={threads}
        actions={actions}
        onSelectThread={onSelectThread}
      />
    );
  }

  if (!active) {
    if (detailQuery.isError) {
      return (
        <ErrorNote>Couldn't load this thread. Try again in a moment.</ErrorNote>
      );
    }
    return detailQuery.isPending ? null : (
      <EmptyState title="Thread not found">
        This thread may have been deleted.
      </EmptyState>
    );
  }

  const turns: ThreadTurn[] =
    detail?.turns && detail.turns.length > 0
      ? detail.turns.map((t, idx) =>
          idx === detail.turns.length - 1 &&
          t.events.length === 0 &&
          detail.events.length > 0
            ? { ...t, events: detail.events }
            : t,
        )
      : [fallbackTurn(active)];

  const waiting =
    active.status === "running" ||
    (active.status === "awaiting_approval" && Boolean(active.permission));

  return (
    <div
      className="thread-chat"
      data-testid={`thread-view-${active.thread_id}`}
    >
      <ol className="chat-turns">
        {turns.map((turn) => (
          <TurnItem key={turn.seq} turn={turn} now={now} />
        ))}
      </ol>
      <div ref={endRef} />

      {waiting && (
        <div className="thread-bottom-stack">
          <ThreadReply
            thread={active}
            elapsed={threadWhen(active, now)}
            actions={actions}
          />
        </div>
      )}
    </div>
  );
}
