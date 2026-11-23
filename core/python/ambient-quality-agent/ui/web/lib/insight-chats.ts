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

import { useQuery } from "@tanstack/react-query";
import { useMemo, useSyncExternalStore } from "react";
import {
  type AgentSelection,
  isConversationShown,
  useAgentSelection,
} from "./agent-selection";
import {
  buildConversationTitle,
  conversationsSnapshot,
  type StoredConversation,
  subscribeConversations,
} from "./aqua-conversations";
import {
  buildLhaContextsQuery,
  isAuthoritativeContextList,
  type StoredContext,
} from "./lha-contexts";
import { isViewerMode } from "./viewer-mode";

const NO_CONVERSATIONS: StoredConversation[] = [];
const getServerSnapshot = () => NO_CONVERSATIONS;

/** One chat conversation associated with an insight. */
export interface InsightChatThread {
  id: string;
  title: string;
  /** Epoch milliseconds of most recent activity, or 0 when unknown. */
  lastUpdated: number;
  /** True when a root-cause diagnosis for this insight was recorded in this chat. */
  hasRootCause: boolean;
}

/**
 * Checks whether a chat context ID still has a live transcript to open.
 *
 * When `/lha/contexts` returned an authoritative server list, a `RootCause`
 * context ID absent from both the server list and browser storage was deleted
 * or expired and should not be offered as a link.
 *
 * @param contextId - Conversation context ID to check.
 * @param conversations - Browser-stored conversations.
 * @param serverContexts - Cached `/lha/contexts` response, if loaded.
 * @param authoritativeServer - Whether `serverContexts` came from an active jobs bucket.
 * @returns True if the conversation can be opened.
 */
export function isChatContextLive(
  contextId: string | undefined | null,
  conversations: readonly StoredConversation[],
  serverContexts: readonly StoredContext[] | undefined,
  authoritativeServer: boolean,
): boolean {
  const id = contextId?.trim();
  if (!id) return false;
  if (!authoritativeServer || !serverContexts) return true;
  return (
    conversations.some((c) => c.id === id) ||
    serverContexts.some((c) => c.context_id === id)
  );
}

/**
 * Merges browser conversations, `/lha/contexts`, and recorded root-cause
 * context IDs into an ordered list of chat threads for `insightId`.
 *
 * @param args - Insight identifier, root-cause context metadata, and conversation sources.
 * @returns Deduplicated chat threads ordered newest first.
 */
export function resolveInsightChats(args: {
  insightId: string;
  rootCauseContextIds?: readonly string[];
  rootCauseTimestamps?: ReadonlyMap<string, number>;
  conversations: readonly StoredConversation[];
  serverContexts: readonly StoredContext[] | undefined;
  authoritativeServer: boolean;
  selection: Pick<AgentSelection, "shownKey" | "defaultKey">;
}): InsightChatThread[] {
  const {
    insightId,
    rootCauseContextIds,
    rootCauseTimestamps,
    conversations,
    serverContexts,
    authoritativeServer,
    selection,
  } = args;
  if (!insightId) return [];

  const localById = new Map(conversations.map((c) => [c.id, c]));
  const remoteById = new Map(
    (serverContexts ?? []).map((c) => [c.context_id, c]),
  );
  const rootCauseIds = new Set(
    (rootCauseContextIds ?? []).map((id) => id.trim()).filter(Boolean),
  );

  const matchedIds = new Set<string>();

  for (const c of conversations) {
    if (c.insightId !== insightId) continue;
    const agentKey = c.agentKey ?? remoteById.get(c.id)?.agent_key ?? undefined;
    if (isConversationShown(agentKey, selection)) {
      matchedIds.add(c.id);
    }
  }

  for (const r of serverContexts ?? []) {
    if (r.insight_id !== insightId) continue;
    const agentKey =
      localById.get(r.context_id)?.agentKey ?? r.agent_key ?? undefined;
    if (isConversationShown(agentKey, selection)) {
      matchedIds.add(r.context_id);
    }
  }

  for (const cid of rootCauseIds) {
    if (
      !isChatContextLive(
        cid,
        conversations,
        serverContexts,
        authoritativeServer,
      )
    ) {
      continue;
    }
    const knownAgent =
      localById.get(cid)?.agentKey ?? remoteById.get(cid)?.agent_key;
    if (
      knownAgent !== undefined &&
      knownAgent !== null &&
      !isConversationShown(knownAgent, selection)
    ) {
      continue;
    }
    matchedIds.add(cid);
  }

  return [...matchedIds]
    .map((id): InsightChatThread => {
      const c = localById.get(id);
      const r = remoteById.get(id);
      const serverStamp = r?.updated_at ? Date.parse(r.updated_at) || 0 : 0;
      const rcStamp = rootCauseTimestamps?.get(id) ?? 0;
      const hasRootCause = rootCauseIds.has(id);
      return {
        id,
        title:
          r?.title ||
          (c && !c.derivedTitle ? c.title : "") ||
          buildConversationTitle(r?.first_message) ||
          c?.title ||
          (hasRootCause ? "Root cause chat" : "Untitled chat"),
        lastUpdated: Math.max(c?.lastUpdated ?? 0, serverStamp, rcStamp),
        hasRootCause,
      };
    })
    .sort((a, b) => {
      if (b.lastUpdated !== a.lastUpdated) return b.lastUpdated - a.lastUpdated;
      if (a.hasRootCause !== b.hasRootCause) return a.hasRootCause ? -1 : 1;
      return a.id.localeCompare(b.id);
    });
}

/**
 * Reactive list of chat threads associated with `insightId`.
 *
 * @param insightId - Target insight identifier.
 * @param rootCauseContextIds - Optional `context_id`s from recorded root causes.
 * @param rootCauseTimestamps - Optional creation timestamps per root-cause `context_id`.
 * @returns Ordered chat threads associated with the insight.
 */
export function useInsightChats(
  insightId: string,
  rootCauseContextIds?: readonly string[],
  rootCauseTimestamps?: ReadonlyMap<string, number>,
): InsightChatThread[] {
  const viewer = isViewerMode();
  const conversations = useSyncExternalStore(
    subscribeConversations,
    conversationsSnapshot,
    getServerSnapshot,
  );
  const hasRootCauseContexts = (rootCauseContextIds?.length ?? 0) > 0;
  // Observe shared `/lha/contexts` cache populated by SidebarRail without
  // firing an extra request on bare list mounts unless root-cause contexts need
  // verification or the user interacts with the control. A read-only viewer
  // serves no conversations.
  const server = useQuery({
    ...buildLhaContextsQuery(),
    enabled: hasRootCauseContexts && !viewer,
  });
  const { shownKey, defaultKey } = useAgentSelection();
  const authoritativeServer = isAuthoritativeContextList(server.data);

  return useMemo(
    () =>
      viewer
        ? []
        : resolveInsightChats({
            insightId,
            rootCauseContextIds,
            rootCauseTimestamps,
            conversations,
            serverContexts: server.data,
            authoritativeServer,
            selection: { shownKey, defaultKey },
          }),
    [
      viewer,
      insightId,
      rootCauseContextIds,
      rootCauseTimestamps,
      conversations,
      server.data,
      authoritativeServer,
      shownKey,
      defaultKey,
    ],
  );
}

/**
 * Checks reactively whether `contextId` has a live conversation to link to.
 *
 * @param contextId - Root-cause conversation context ID.
 * @returns True if `contextId` is non-empty, not in viewer mode, and has not
 *   been deleted on the server.
 */
export function useIsChatContextLive(
  contextId: string | undefined | null,
): boolean {
  const viewer = isViewerMode();
  const conversations = useSyncExternalStore(
    subscribeConversations,
    conversationsSnapshot,
    getServerSnapshot,
  );
  const server = useQuery({
    ...buildLhaContextsQuery(),
    enabled: Boolean(contextId?.trim()) && !viewer,
  });
  if (viewer) return false;
  return isChatContextLive(
    contextId,
    conversations,
    server.data,
    isAuthoritativeContextList(server.data),
  );
}
