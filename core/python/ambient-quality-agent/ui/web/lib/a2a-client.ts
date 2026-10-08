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

// Thin wrapper around @a2a-js/sdk that fetches the agent card and exposes
// sendMessageStream()/getTask()/resubscribeTask() over same-origin /a2a.
//
// Same-origin requests to FastAPI: agent card at /.well-known/agent-card.json,
// RPC at /a2a. In dev, the Vite proxy (vite.config.ts) forwards these to the
// backend on :8001; in production the web/server/ Express proxy forwards them
// to the backend.

import { ClientFactory, JsonRpcTransportFactory } from "@a2a-js/sdk/client";
import type { Client } from "@a2a-js/sdk/client";
import type { AgentCard, Part, Task } from "@a2a-js/sdk";
import { newId } from "./new-id";
import { makeDataPart, makeTextPart, makeUserMessage } from "./a2a-part";

const RPC_PATH = "/a2a";

export interface HorizonClient {
  card: AgentCard;
  client: Client;
  sendStream: (
    text: string,
    opts?: SendOptions,
  ) => AsyncGenerator<unknown, void, void>;
  sendConfirmation: (
    opts: SendConfirmationOptions,
  ) => AsyncGenerator<unknown, void, void>;
  sendConfirmations: (
    items: ConfirmationItem[],
    opts?: { signal?: AbortSignal; messageId?: string },
  ) => AsyncGenerator<unknown, void, void>;
  getTask: (taskId: string, opts?: { signal?: AbortSignal }) => Promise<Task>;
  cancelTask: (
    taskId: string,
    opts?: { signal?: AbortSignal },
  ) => Promise<void>;
  resubscribeTask: (
    taskId: string,
    opts?: { signal?: AbortSignal },
  ) => AsyncGenerator<unknown, void, void>;
  contextId: string;
}

export interface SendConfirmationOptions {
  /** The function_call id the agent is awaiting a response for. */
  callId: string;
  /** True if the user approved (clarify choice picked or Approve clicked). */
  confirmed: boolean;
  /**
   * For clarify-style confirmations, the picked choice or free-form answer.
   * For yes/no confirmations, any extra payload the original confirmation
   * carried (or null).
   */
  payload?: Record<string, unknown> | null;
  signal?: AbortSignal;
  messageId?: string;
  /** Optional original tool name for the function_response envelope. */
  toolName?: string;
}

export interface ConfirmationItem {
  callId: string;
  confirmed: boolean;
  payload?: Record<string, unknown> | null;
  toolName?: string;
}

export function buildConfirmationDataPart(item: ConfirmationItem): Part {
  return makeDataPart(
    {
      id: item.callId,
      name: item.toolName ?? "adk_request_confirmation",
      // ADK's `_parse_tool_confirmation` expects the ToolConfirmation fields
      // directly (confirmed/payload), not wrapped under a `toolConfirmation` key.
      response: {
        confirmed: item.confirmed,
        payload: item.payload ?? null,
      },
    },
    // Part-level metadata, NOT part of the data payload — the backend keys off
    // this marker to recognise the function response.
    { adk_type: "function_response" },
  );
}

export interface CreateLhaClientOptions {
  contextId: string;
  /** Aborts the agent-card retry loop (e.g. when the boot effect tears down). */
  signal?: AbortSignal;
}

export interface SendOptions {
  /** Override the auto-generated message id. */
  messageId?: string;
  /** Abort signal forwarded to the underlying SDK request. */
  signal?: AbortSignal;
  /** Additional Parts appended after the text part (e.g., attachment files). */
  extraParts?: Part[];
}

// Per-attempt cap. The backend Cloud Run service runs min-instances=1, but a
// just-deployed revision or a scale-up instance (1→max) can take tens of
// seconds to serve its first request while the ADK app finishes init, so this
// is generous; callers retry around it and keep boot in the "connecting" state
// meanwhile.
const AGENT_CARD_TIMEOUT_MS = 20_000;
const AGENT_CARD_MAX_ATTEMPTS = 5;
const AGENT_CARD_RETRY_BASE_MS = 1_500;

function abortError(): DOMException {
  return new DOMException("aborted", "AbortError");
}

function sleep(ms: number, signal?: AbortSignal): Promise<void> {
  return new Promise((resolve, reject) => {
    if (signal?.aborted) {
      reject(abortError());
      return;
    }
    const t = setTimeout(resolve, ms);
    signal?.addEventListener(
      "abort",
      () => {
        clearTimeout(t);
        reject(abortError());
      },
      { once: true },
    );
  });
}

let cachedAgentCard: { fetchRef: typeof fetch; card: AgentCard } | null = null;
let inFlightAgentCard: {
  fetchRef: typeof fetch;
  promise: Promise<AgentCard>;
} | null = null;

function isValidAgentCard(card: unknown): card is AgentCard {
  return (
    typeof card === "object" &&
    card !== null &&
    Array.isArray((card as AgentCard).supportedInterfaces) &&
    (card as AgentCard).supportedInterfaces.length > 0
  );
}

export async function fetchAgentCard(opts?: {
  timeoutMs?: number;
  signal?: AbortSignal;
}): Promise<AgentCard> {
  if (opts?.signal?.aborted) {
    throw new DOMException("The operation was aborted.", "AbortError");
  }
  const currentFetch = globalThis.fetch;
  if (cachedAgentCard && cachedAgentCard.fetchRef === currentFetch) {
    return cachedAgentCard.card;
  }
  if (!inFlightAgentCard || inFlightAgentCard.fetchRef !== currentFetch) {
    // Hard cap the lookup so a backend hang doesn't freeze boot indefinitely
    // (the chat input is disabled until the client resolves).
    const timeoutMs = opts?.timeoutMs ?? AGENT_CARD_TIMEOUT_MS;
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), timeoutMs);
    const run = async (): Promise<AgentCard> => {
      try {
        const res = await currentFetch(`/.well-known/agent-card.json`, {
          cache: "no-store",
          signal: controller.signal,
        });
        if (!res.ok) {
          throw new Error(
            `failed to fetch agent card: ${res.status} ${res.statusText}`,
          );
        }
        const card = (await res.json()) as AgentCard;
        if (isValidAgentCard(card)) {
          cachedAgentCard = { fetchRef: currentFetch, card };
        }
        return card;
      } catch (err) {
        if (err instanceof DOMException && err.name === "AbortError") {
          throw new Error(`agent card fetch timed out after ${timeoutMs}ms`);
        }
        throw err;
      } finally {
        clearTimeout(timeout);
        if (inFlightAgentCard?.promise === promise) {
          inFlightAgentCard = null;
        }
      }
    };
    const promise = run();
    inFlightAgentCard = { fetchRef: currentFetch, promise };
  }
  const sharedPromise = inFlightAgentCard.promise;
  if (!opts?.signal) {
    return sharedPromise;
  }
  const signal = opts.signal;
  return new Promise<AgentCard>((resolve, reject) => {
    const onAbort = () => {
      reject(new DOMException("The operation was aborted.", "AbortError"));
    };
    signal.addEventListener("abort", onAbort, { once: true });
    sharedPromise.then(
      (val) => {
        signal.removeEventListener("abort", onAbort);
        resolve(val);
      },
      (err) => {
        signal.removeEventListener("abort", onAbort);
        reject(err);
      },
    );
  });
}

/** Warm the agent card cache in the background so opening Chat needs no network round-trip. */
export function prefetchAgentCard(): void {
  if (typeof globalThis.fetch !== "function") return;
  void fetchAgentCard().catch(() => {});
}

// Keep retrying with linear backoff while a new or scaling-up instance warms
// up, so boot shows a "connecting" spinner instead of flipping to
// "disconnected" on the first slow response. Stops immediately if the caller
// aborts.
async function fetchAgentCardWithRetry(
  signal?: AbortSignal,
): Promise<AgentCard> {
  let lastErr: unknown;
  for (let attempt = 1; attempt <= AGENT_CARD_MAX_ATTEMPTS; attempt++) {
    if (signal?.aborted) throw abortError();
    try {
      return await fetchAgentCard({ signal });
    } catch (err) {
      if (signal?.aborted) throw err;
      lastErr = err;
      if (attempt < AGENT_CARD_MAX_ATTEMPTS) {
        await sleep(AGENT_CARD_RETRY_BASE_MS * attempt, signal);
      }
    }
  }
  throw lastErr;
}

// An empty text part — which happens when a file is sent with no caption — can
// be rejected by strict model backends; only include the text part when it
// carries content.
export function buildMessageParts(text: string, extraParts?: Part[]): Part[] {
  const parts: Part[] = [];
  if (text.trim()) parts.push(makeTextPart(text));
  if (extraParts?.length) parts.push(...extraParts);
  return parts;
}

export async function createLhaClient(
  opts: CreateLhaClientOptions,
): Promise<HorizonClient> {
  const card = await fetchAgentCardWithRetry(opts.signal);
  // Without this the factory fails with an opaque "no compatible transport",
  // which reads as a generic boot failure rather than a version mismatch.
  if (!card.supportedInterfaces?.length) {
    throw new Error(
      "agent card declares no supportedInterfaces — backend predates A2A 1.0",
    );
  }
  // Force the RPC URL to a relative same-origin path. The agent card built by
  // ADK declares an absolute URL based on APP_URL, which may not match the
  // browser's current origin (and in dev, the Vite proxy forwards /a2a to the
  // backend on :8001).
  const proxiedCard: AgentCard = {
    ...card,
    supportedInterfaces: card.supportedInterfaces.map((i) => ({
      ...i,
      url: RPC_PATH,
    })),
  };

  const { contextId } = opts;

  // `legacyCompat` is deliberately omitted: it defaults to disabled, which
  // selects the native transport. Our card also advertises a compatibility
  // interface for Gemini; enabling compat here would route the
  // browser back onto that wire.
  // `fetch` must stay bound to the global; the transport invokes it as
  // `this.fetchImpl(...)`, and a bare reference throws "Illegal invocation".
  const client = await new ClientFactory({
    transports: [
      new JsonRpcTransportFactory({ fetchImpl: (...a) => fetch(...a) }),
    ],
  }).createFromAgentCard(proxiedCard);

  async function* sendStream(
    text: string,
    opts: SendOptions = {},
  ): AsyncGenerator<unknown, void, void> {
    const message = makeUserMessage({
      messageId: opts.messageId ?? newId(),
      contextId,
      parts: buildMessageParts(text, opts.extraParts),
    });
    for await (const event of client.sendMessageStream(
      { tenant: "", message, configuration: undefined, metadata: undefined },
      { signal: opts.signal },
    )) {
      yield event;
    }
  }

  async function* sendConfirmation(
    opts: SendConfirmationOptions,
  ): AsyncGenerator<unknown, void, void> {
    const message = makeUserMessage({
      messageId: opts.messageId ?? newId(),
      contextId,
      parts: [buildConfirmationDataPart(opts)],
    });
    for await (const event of client.sendMessageStream(
      { tenant: "", message, configuration: undefined, metadata: undefined },
      { signal: opts.signal },
    )) {
      yield event;
    }
  }

  async function* sendConfirmations(
    items: ConfirmationItem[],
    opts: { signal?: AbortSignal; messageId?: string } = {},
  ): AsyncGenerator<unknown, void, void> {
    const message = makeUserMessage({
      messageId: opts.messageId ?? newId(),
      contextId,
      parts: items.map(buildConfirmationDataPart),
    });
    for await (const event of client.sendMessageStream(
      { tenant: "", message, configuration: undefined, metadata: undefined },
      { signal: opts.signal },
    )) {
      yield event;
    }
  }

  async function getTask(
    taskId: string,
    opts: { signal?: AbortSignal } = {},
  ): Promise<Task> {
    return client.getTask(
      { tenant: "", id: taskId, historyLength: undefined },
      { signal: opts.signal },
    );
  }

  // Errors propagate to callers — an "already terminal" rejection on a completed task is benign.
  async function cancelTask(
    taskId: string,
    opts: { signal?: AbortSignal } = {},
  ): Promise<void> {
    await client.cancelTask(
      { tenant: "", id: taskId, metadata: undefined },
      { signal: opts.signal },
    );
  }

  async function* resubscribeTask(
    taskId: string,
    opts: { signal?: AbortSignal } = {},
  ): AsyncGenerator<unknown, void, void> {
    for await (const event of client.resubscribeTask(
      { tenant: "", id: taskId },
      { signal: opts.signal },
    )) {
      yield event;
    }
  }

  return {
    card,
    client,
    sendStream,
    sendConfirmation,
    sendConfirmations,
    getTask,
    cancelTask,
    resubscribeTask,
    contextId,
  };
}
