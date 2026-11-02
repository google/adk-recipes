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
  type EpisodeMemory,
  useEpisodeMemories,
  useMemories,
  useOrchestratorActions,
} from "../../../lib/api";
import { Section } from "../../common/primitives";
import { DeleteButton, EmptyState, ErrorNote, Row } from "../../common/ui";

/**
 * "What ADK Voice Agent remembers": the user's profile, one row per field, each one deletable,
 * then what it remembers of recent conversations and threads.
 */
export function MemorySection() {
  const { data, isError, isPending } = useMemories();
  const { forgetMemory } = useOrchestratorActions();
  const memories = data?.memories ?? [];

  return (
    <>
      <Section label="What ADK Voice Agent Remembers">
        {isError ? (
          <ErrorNote>Couldn't load what ADK Voice Agent remembers.</ErrorNote>
        ) : isPending ? (
          <div className="connections-note">Loading…</div>
        ) : memories.length === 0 ? (
          <EmptyState title="Nothing yet">
            As you talk, ADK Voice Agent keeps a short profile of you, like your
            name, your projects, and how you like replies, so it can tailor
            answers and threads.
          </EmptyState>
        ) : (
          memories.map((m) => (
            <Row
              key={m.id}
              testId={`memory-${m.field}`}
              title={m.label}
              subtitle={m.value}
              wrap
              trailing={
                <DeleteButton
                  label={`Forget ${m.label.toLowerCase()}`}
                  onDelete={() => forgetMemory.mutate(m.id)}
                />
              }
            />
          ))
        )}
        {forgetMemory.isError && (
          <ErrorNote>{forgetMemory.error.message}</ErrorNote>
        )}
      </Section>
      <EpisodeSections />
    </>
  );
}

const EPISODE_SECTIONS: Array<{
  kind: EpisodeMemory["kind"];
  label: string;
  empty: string;
}> = [
  {
    kind: "conversation",
    label: "Recent Conversations",
    empty:
      "When a conversation ends, ADK Voice Agent keeps a few sentences about it so it can pick up where you left off.",
  },
  {
    kind: "thread",
    label: "Past Threads",
    empty:
      "ADK Voice Agent remembers each thread by your request, so you can come back to it later. Deleting a thread forgets it.",
  },
];

/** Remembered conversations and threads, each deletable. */
function EpisodeSections() {
  const { data, isError, isPending } = useEpisodeMemories();
  const { forgetEpisode } = useOrchestratorActions();

  return (
    <>
      {EPISODE_SECTIONS.map(({ kind, label, empty }) => {
        const items = data?.[kind] ?? [];
        return (
          <Section key={kind} label={label}>
            {isError ? (
              <ErrorNote>Couldn't load these memories.</ErrorNote>
            ) : isPending ? (
              <div className="connections-note">Loading…</div>
            ) : items.length === 0 ? (
              <EmptyState title="Nothing yet">{empty}</EmptyState>
            ) : (
              items.map((e) => (
                <Row
                  key={e.id}
                  testId={`episode-${e.id}`}
                  title={e.fact}
                  wrap
                  trailing={
                    <DeleteButton
                      label={`Forget this ${kind}`}
                      onDelete={() => forgetEpisode.mutate({ kind, id: e.id })}
                    />
                  }
                />
              ))
            )}
          </Section>
        );
      })}
      {forgetEpisode.isError && (
        <ErrorNote>{forgetEpisode.error.message}</ErrorNote>
      )}
    </>
  );
}
