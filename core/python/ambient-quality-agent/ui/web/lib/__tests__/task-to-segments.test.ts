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

import { describe, expect, it } from "vitest";
import { Role, TaskState } from "@a2a-js/sdk";
import type { Message, Part, Task } from "@a2a-js/sdk";

import type { ChatMessage } from "@/components/chat/chat-shell";

// A2A 1.0 part fixtures. Parts are a ts-proto tagged union under `content`
// and carry required metadata/filename/mediaType, so build them here rather
// than repeating the shape at ~110 call sites.
function tp(text: string): Part {
  return {
    content: { $case: "text", value: text },
    metadata: undefined,
    filename: "",
    mediaType: "",
  };
}

function dp(data: unknown, metadata?: Record<string, unknown>): Part {
  return {
    content: { $case: "data", value: data },
    metadata,
    filename: "",
    mediaType: "",
  };
}
import {
  resolveHitlAcrossTasks,
  resolveToolResultsAcrossTasks,
  taskToChatMessages,
} from "@/lib/task-to-segments";

// Fixture helper: fills required Message fields that fixtures omit (contextId,
// taskId, metadata, extensions, referenceTaskIds) and converts string role to
// the numeric 1.0 enum so the cast to Message[] typechecks without widening.
function msg(m: {
  messageId: string;
  role: "user" | "agent";
  parts: Part[];
  metadata?: Record<string, unknown>;
}): Message {
  return {
    messageId: m.messageId,
    contextId: "ctx-1",
    taskId: "task-1",
    role: m.role === "user" ? Role.ROLE_USER : Role.ROLE_AGENT,
    parts: m.parts,
    metadata: m.metadata,
    extensions: [],
    referenceTaskIds: [],
  };
}

function makeTask(history: Message[]): Task {
  return {
    id: "task-1",
    contextId: "ctx-1",
    status: { state: "completed" },
    history,
  } as unknown as Task;
}

describe("taskToChatMessages", () => {
  it("drops adk_partial=true messages so the final aggregated event is rendered once", () => {
    // ADK with SSE streaming emits N partial token deltas (adk_partial=true)
    // and one final aggregated message (adk_partial absent or false) carrying
    // the full text. On replay we want exactly one bubble with the full text.
    const task = makeTask([
      msg({
        messageId: "p1",
        role: "agent",
        metadata: { adk_partial: true },
        parts: [tp("I'm doing well, ")],
      }),
      msg({
        messageId: "p2",
        role: "agent",
        metadata: { adk_partial: true },
        parts: [tp("thank you! ")],
      }),
      msg({
        messageId: "p3",
        role: "agent",
        metadata: { adk_partial: true },
        parts: [tp("How can I help you today?")],
      }),
      msg({
        messageId: "final",
        role: "agent",
        parts: [tp("I'm doing well, thank you! How can I help you today?")],
      }),
    ]);

    const messages = taskToChatMessages({
      task,
      taskIndex: 0,
    });

    expect(messages).toHaveLength(1);
    expect(messages[0].id).toBe("final");
    expect(messages[0].segments).toEqual([
      {
        kind: "text",
        text: "I'm doing well, thank you! How can I help you today?",
      },
    ]);
  });

  it("attaches a function_response to its function_call across separate history messages", () => {
    // ADK splits one assistant turn into separate history messages: the
    // function_call, the function_response, and the final text each arrive as
    // their own `role: "agent"` message (the response carries no id — see the
    // function-response-dropped fixture). On replay they must collapse into one
    // assistant bubble so the tool_result attaches to its tool segment;
    // otherwise the terminal bubble renders a permanent "running…" spinner.
    const task = makeTask([
      msg({ messageId: "u1", role: "user", parts: [tp("run ls")] }),
      msg({
        messageId: "call",
        role: "agent",
        parts: [
          dp(
            { id: "call-1", name: "terminal", args: { command: "ls" } },
            { adk_type: "function_call" },
          ),
        ],
      }),
      msg({
        messageId: "resp",
        role: "agent",
        parts: [
          dp(
            { name: "terminal", response: { stdout: "a\nb", exit_code: 0 } },
            { adk_type: "function_response" },
          ),
        ],
      }),
      msg({ messageId: "final", role: "agent", parts: [tp("Done.")] }),
    ]);

    const messages = taskToChatMessages({
      task,
      taskIndex: 0,
    });

    expect(messages).toHaveLength(2);
    expect(messages[0].role).toBe("user");
    const assistant = messages[1];
    expect(assistant.role).toBe("assistant");
    const tool = assistant.segments.find((s) => s.kind === "tool");
    expect(tool).toMatchObject({
      kind: "tool",
      name: "terminal",
      hasResult: true,
      result: { stdout: "a\nb", exit_code: 0 },
    });
    expect(assistant.segments).toContainEqual({ kind: "text", text: "Done." });
  });

  it("flushes a pending agent run before a following user message (order preserved)", () => {
    const task = makeTask([
      msg({ messageId: "u1", role: "user", parts: [tp("q1")] }),
      msg({
        messageId: "call",
        role: "agent",
        parts: [
          dp(
            { id: "c1", name: "terminal", args: { command: "ls" } },
            { adk_type: "function_call" },
          ),
        ],
      }),
      msg({
        messageId: "resp",
        role: "agent",
        parts: [
          dp(
            { name: "terminal", response: { stdout: "ok", exit_code: 0 } },
            { adk_type: "function_response" },
          ),
        ],
      }),
      msg({ messageId: "u2", role: "user", parts: [tp("q2")] }),
      msg({ messageId: "a2", role: "agent", parts: [tp("answer")] }),
    ]);

    const messages = taskToChatMessages({
      task,
      taskIndex: 0,
    });

    expect(messages.map((m) => m.role)).toEqual([
      "user",
      "assistant",
      "user",
      "assistant",
    ]);
    expect(messages[1].segments).toContainEqual(
      expect.objectContaining({
        kind: "tool",
        name: "terminal",
        hasResult: true,
      }),
    );
    expect(messages[3].segments).toContainEqual({
      kind: "text",
      text: "answer",
    });
  });

  it("attaches each result to its own tool segment when one agent run has two calls", () => {
    const task = makeTask([
      msg({ messageId: "u1", role: "user", parts: [tp("two")] }),
      msg({
        messageId: "callA",
        role: "agent",
        parts: [
          dp(
            { id: "a", name: "terminal", args: { command: "echo a" } },
            { adk_type: "function_call" },
          ),
        ],
      }),
      msg({
        messageId: "respA",
        role: "agent",
        parts: [
          dp(
            {
              id: "a",
              name: "terminal",
              response: { stdout: "a", exit_code: 0 },
            },
            { adk_type: "function_response" },
          ),
        ],
      }),
      msg({
        messageId: "callB",
        role: "agent",
        parts: [
          dp(
            { id: "b", name: "terminal", args: { command: "echo b" } },
            { adk_type: "function_call" },
          ),
        ],
      }),
      msg({
        messageId: "respB",
        role: "agent",
        parts: [
          dp(
            {
              id: "b",
              name: "terminal",
              response: { stdout: "b", exit_code: 0 },
            },
            { adk_type: "function_response" },
          ),
        ],
      }),
    ]);

    const messages = taskToChatMessages({
      task,
      taskIndex: 0,
    });

    expect(messages).toHaveLength(2);
    const tools = messages[1].segments.filter((s) => s.kind === "tool");
    expect(tools).toHaveLength(2);
    expect(tools).toContainEqual(
      expect.objectContaining({
        callId: "a",
        hasResult: true,
        result: { stdout: "a", exit_code: 0 },
      }),
    );
    expect(tools).toContainEqual(
      expect.objectContaining({
        callId: "b",
        hasResult: true,
        result: { stdout: "b", exit_code: 0 },
      }),
    );
  });

  it("stamps every message with the task id and strictly-increasing createdAt", () => {
    const task = makeTask([
      msg({ messageId: "u1", role: "user", parts: [tp("hi")] }),
      msg({ messageId: "a1", role: "agent", parts: [tp("yo")] }),
    ]);

    const messages = taskToChatMessages({
      task,
      taskIndex: 0,
    });

    // taskId lets the history↔live merge drop optimistic bubbles once their
    // canonical copy lands in history.
    expect(messages.every((m) => m.taskId === "task-1")).toBe(true);
    // createdAt is the load-bearing sort key — it must be monotonic per task.
    for (let i = 1; i < messages.length; i++) {
      expect(messages[i].createdAt).toBeGreaterThan(messages[i - 1].createdAt);
    }
  });

  it("keeps non-partial messages untouched", () => {
    const task = makeTask([
      msg({ messageId: "u1", role: "user", parts: [tp("how are you")] }),
      msg({ messageId: "a1", role: "agent", parts: [tp("Doing great.")] }),
    ]);

    const messages = taskToChatMessages({
      task,
      taskIndex: 0,
    });

    expect(messages).toHaveLength(2);
    expect(messages[0].role).toBe("user");
    expect(messages[1].role).toBe("assistant");
  });

  it("folds artifact text into the turn's assistant bubble (new-impl path)", () => {
    // New-impl path: tool calls land in history while the model's text streams
    // as artifacts. They must collapse into one assistant bubble (chip + text),
    // not split into a tool bubble and a separate text bubble.
    const task = {
      id: "task-1",
      contextId: "ctx-1",
      status: { state: "completed" },
      history: [
        { messageId: "u1", role: "user", parts: [tp("run ls")] },
        {
          messageId: "call",
          role: "agent",
          parts: [
            dp(
              { id: "c1", name: "terminal", args: { command: "ls" } },
              { adk_type: "function_call" },
            ),
          ],
        },
      ],
      artifacts: [{ artifactId: "art-1", parts: [tp("Here is the listing.")] }],
    } as unknown as Task;

    const messages = taskToChatMessages({ task, taskIndex: 0 });

    expect(messages).toHaveLength(2);
    const assistant = messages[1];
    expect(assistant.role).toBe("assistant");
    expect(
      assistant.segments.some(
        (s) => s.kind === "tool" && s.name === "terminal",
      ),
    ).toBe(true);
    expect(assistant.segments).toContainEqual({
      kind: "text",
      text: "Here is the listing.",
    });
  });

  it("renders a text-only turn from artifacts when history carries no model text", () => {
    const task = {
      id: "task-1",
      contextId: "ctx-1",
      status: { state: "completed" },
      history: [{ messageId: "u1", role: "user", parts: [tp("hi")] }],
      artifacts: [{ artifactId: "art-1", parts: [tp("Hello there.")] }],
    } as unknown as Task;

    const messages = taskToChatMessages({ task, taskIndex: 0 });

    expect(messages.map((m) => m.role)).toEqual(["user", "assistant"]);
    expect(messages[1].segments).toEqual([
      { kind: "text", text: "Hello there." },
    ]);
  });

  it("renders the HITL confirmation card from status.message on an input-required task", () => {
    // The new-impl executor emits adk_request_confirmation as the input-required
    // status (final), which A2A stores in status.message — not history. Replay
    // must surface it or the live card vanishes when the post-turn refetch
    // replaces the bubble.
    const task = {
      id: "task-1",
      contextId: "ctx-1",
      status: {
        state: TaskState.TASK_STATE_INPUT_REQUIRED,
        message: {
          role: "agent",
          parts: [
            dp(
              {
                id: "adk-conf-9",
                name: "adk_request_confirmation",
                args: {
                  originalFunctionCall: {
                    id: "t1",
                    name: "terminal",
                    args: { command: "uv run x" },
                  },
                  toolConfirmation: {
                    hint: "Run: uv run x",
                    payload: {
                      kind: "permission",
                      choices: ["Yes, once", "Decline"],
                    },
                  },
                },
              },
              { adk_type: "function_call" },
            ),
          ],
        },
      },
      history: [
        { messageId: "u1", role: "user", parts: [tp("run it")] },
        {
          messageId: "call",
          role: "agent",
          parts: [
            dp(
              { id: "t1", name: "terminal", args: { command: "uv run x" } },
              { adk_type: "function_call" },
            ),
          ],
        },
      ],
    } as unknown as Task;

    const conf = taskToChatMessages({ task, taskIndex: 0 })
      .flatMap((m) => m.segments)
      .find((s) => s.kind === "confirmation");
    expect(conf).toMatchObject({ kind: "confirmation", callId: "adk-conf-9" });
  });

  it("renders a failed task's error message from status.message", () => {
    // A failed turn carries its error in status.message and nowhere else; the
    // live stream shows it, so replay must too, or the post-turn refetch
    // replaces the error with nothing.
    const task = {
      id: "task-1",
      contextId: "ctx-1",
      status: {
        state: TaskState.TASK_STATE_FAILED,
        message: {
          role: "agent",
          parts: [tp("The model request failed (400 INVALID_ARGUMENT).")],
        },
      },
      history: [{ messageId: "u1", role: "user", parts: [tp("save it")] }],
    } as unknown as Task;

    const messages = taskToChatMessages({ task, taskIndex: 0 });

    expect(messages.map((m) => m.role)).toEqual(["user", "assistant"]);
    expect(messages[1].segments).toEqual([
      {
        kind: "text",
        text: "The model request failed (400 INVALID_ARGUMENT).",
      },
    ]);
  });
});

describe("taskToChatMessages — HITL resolution on replay", () => {
  it("resolves a confirmation from the user's approve echo (function_response)", () => {
    const task = makeTask([
      msg({ messageId: "u1", role: "user", parts: [tp("run ls")] }),
      msg({
        messageId: "conf",
        role: "agent",
        parts: [
          dp(
            {
              id: "adk-conf-2",
              name: "adk_request_confirmation",
              args: {
                originalFunctionCall: {
                  id: "call-terminal-1",
                  name: "terminal",
                  args: { command: "ls /tmp" },
                },
                toolConfirmation: {
                  hint: "Run shell command: ls /tmp ?",
                  payload: null,
                },
              },
            },
            { adk_type: "function_call" },
          ),
        ],
      }),
      msg({
        messageId: "echo",
        role: "user",
        parts: [
          dp(
            {
              id: "adk-conf-2",
              name: "adk_request_confirmation",
              response: {
                hint: "Run shell command: ls /tmp ?",
                confirmed: true,
                payload: null,
              },
            },
            { adk_type: "function_response" },
          ),
        ],
      }),
    ]);

    const messages = taskToChatMessages({
      task,
      taskIndex: 0,
    });

    const conf = messages
      .flatMap((m) => m.segments)
      .find((s) => s.kind === "confirmation");
    expect(conf).toMatchObject({
      kind: "confirmation",
      callId: "adk-conf-2",
      answer: { confirmed: true, text: null },
    });
  });

  it("resolves a declined confirmation from the user's echo", () => {
    const task = makeTask([
      msg({
        messageId: "conf",
        role: "agent",
        parts: [
          dp(
            {
              id: "adk-conf-3",
              name: "adk_request_confirmation",
              args: {
                originalFunctionCall: {
                  id: "t1",
                  name: "terminal",
                  args: null,
                },
                toolConfirmation: { hint: "Do it?", payload: null },
              },
            },
            { adk_type: "function_call" },
          ),
        ],
      }),
      msg({
        messageId: "echo",
        role: "user",
        parts: [
          dp(
            {
              id: "adk-conf-3",
              name: "adk_request_confirmation",
              response: { hint: "Do it?", confirmed: false, payload: null },
            },
            { adk_type: "function_response" },
          ),
        ],
      }),
    ]);

    const conf = taskToChatMessages({
      task,
      taskIndex: 0,
    })
      .flatMap((m) => m.segments)
      .find((s) => s.kind === "confirmation");
    expect(conf).toMatchObject({ answer: { confirmed: false, text: null } });
  });

  it("resolves a clarify card with the chosen answer from the echo payload", () => {
    const task = makeTask([
      msg({
        messageId: "conf",
        role: "agent",
        parts: [
          dp(
            {
              id: "adk-conf-1",
              name: "adk_request_confirmation",
              args: {
                originalFunctionCall: {
                  id: "clar-1",
                  name: "clarify",
                  args: { question: "Which plot?", choices: ["line", "bar"] },
                },
                toolConfirmation: {
                  hint: "Which plot?",
                  payload: {
                    question: "Which plot?",
                    choices: ["line", "bar"],
                  },
                },
              },
            },
            { adk_type: "function_call" },
          ),
        ],
      }),
      msg({
        messageId: "echo",
        role: "user",
        parts: [
          dp(
            {
              id: "adk-conf-1",
              name: "adk_request_confirmation",
              response: {
                hint: "Which plot?",
                confirmed: true,
                payload: { choice: "bar" },
              },
            },
            { adk_type: "function_response" },
          ),
        ],
      }),
    ]);

    const clar = taskToChatMessages({
      task,
      taskIndex: 0,
    })
      .flatMap((m) => m.segments)
      .find((s) => s.kind === "clarify");
    expect(clar).toMatchObject({
      kind: "clarify",
      callId: "adk-conf-1",
      answer: { confirmed: true, text: "bar" },
    });
  });

  it("handles the ADK client {response: jsonString} wrapper shape", () => {
    const task = makeTask([
      msg({
        messageId: "conf",
        role: "agent",
        parts: [
          dp(
            {
              id: "adk-conf-4",
              name: "adk_request_confirmation",
              args: {
                originalFunctionCall: {
                  id: "t2",
                  name: "terminal",
                  args: null,
                },
                toolConfirmation: { hint: "Do it?", payload: null },
              },
            },
            { adk_type: "function_call" },
          ),
        ],
      }),
      msg({
        messageId: "echo",
        role: "user",
        parts: [
          dp(
            {
              id: "adk-conf-4",
              name: "adk_request_confirmation",
              response: {
                response: JSON.stringify({
                  hint: "Do it?",
                  confirmed: true,
                  payload: null,
                }),
              },
            },
            { adk_type: "function_response" },
          ),
        ],
      }),
    ]);

    const conf = taskToChatMessages({
      task,
      taskIndex: 0,
    })
      .flatMap((m) => m.segments)
      .find((s) => s.kind === "confirmation");
    expect(conf).toMatchObject({ answer: { confirmed: true, text: null } });
  });

  it("resolves a declined confirmation from the {response: jsonString} wrapper shape", () => {
    const task = makeTask([
      msg({
        messageId: "conf",
        role: "agent",
        parts: [
          dp(
            {
              id: "adk-conf-5",
              name: "adk_request_confirmation",
              args: {
                originalFunctionCall: {
                  id: "t3",
                  name: "terminal",
                  args: null,
                },
                toolConfirmation: { hint: "Do it?", payload: null },
              },
            },
            { adk_type: "function_call" },
          ),
        ],
      }),
      msg({
        messageId: "echo",
        role: "user",
        parts: [
          dp(
            {
              id: "adk-conf-5",
              name: "adk_request_confirmation",
              response: {
                response: JSON.stringify({
                  hint: "Do it?",
                  confirmed: false,
                  payload: null,
                }),
              },
            },
            { adk_type: "function_response" },
          ),
        ],
      }),
    ]);

    const conf = taskToChatMessages({
      task,
      taskIndex: 0,
    })
      .flatMap((m) => m.segments)
      .find((s) => s.kind === "confirmation");
    expect(conf).toMatchObject({ answer: { confirmed: false, text: null } });
  });

  it("extracts the chosen answer from the {response: jsonString} wrapper payload", () => {
    const task = makeTask([
      msg({
        messageId: "conf",
        role: "agent",
        parts: [
          dp(
            {
              id: "adk-conf-6",
              name: "adk_request_confirmation",
              args: {
                originalFunctionCall: {
                  id: "clar-2",
                  name: "clarify",
                  args: { question: "Which plot?", choices: ["line", "bar"] },
                },
                toolConfirmation: {
                  hint: "Which plot?",
                  payload: {
                    question: "Which plot?",
                    choices: ["line", "bar"],
                  },
                },
              },
            },
            { adk_type: "function_call" },
          ),
        ],
      }),
      msg({
        messageId: "echo",
        role: "user",
        parts: [
          dp(
            {
              id: "adk-conf-6",
              name: "adk_request_confirmation",
              response: {
                response: JSON.stringify({
                  hint: "Which plot?",
                  confirmed: true,
                  payload: { choice: "bar" },
                }),
              },
            },
            { adk_type: "function_response" },
          ),
        ],
      }),
    ]);

    const clar = taskToChatMessages({
      task,
      taskIndex: 0,
    })
      .flatMap((m) => m.segments)
      .find((s) => s.kind === "clarify");
    expect(clar).toMatchObject({
      kind: "clarify",
      callId: "adk-conf-6",
      answer: { confirmed: true, text: "bar" },
    });
  });
});

describe("resolveHitlAcrossTasks", () => {
  // ADK resumes an approved tool call into a NEW A2A task, so a confirmation
  // card lands in one task while its approve/decline echo lands in the next.
  // A per-task resolve leaves the card stuck on its live controls after reload;
  // this cross-task pass matches the echo back to the card by callId.
  function cardOnlyMessages(callId: string): ChatMessage[] {
    return [
      {
        id: "m1",
        role: "assistant",
        createdAt: 1,
        taskId: "task-card",
        segments: [
          {
            kind: "confirmation",
            callId,
            hint: "Run rm -rf?",
            originalCall: { name: "terminal", args: { command: "rm -rf x" } },
            payload: null,
          },
        ],
      },
    ];
  }

  function echoTask(callId: string, confirmed: boolean): Task {
    return makeTask([
      msg({
        messageId: "echo",
        role: "user",
        parts: [
          dp(
            {
              id: callId,
              name: "adk_request_confirmation",
              response: { confirmed, payload: null },
            },
            { adk_type: "function_response" },
          ),
        ],
      }),
    ]);
  }

  it("resolves a confirmation card whose approve echo lives in a later task", () => {
    const resolved = resolveHitlAcrossTasks(cardOnlyMessages("adk-x"), [
      echoTask("adk-x", true),
    ]);
    const conf = resolved
      .flatMap((m) => m.segments)
      .find((s) => s.kind === "confirmation");
    expect(conf).toMatchObject({
      callId: "adk-x",
      answer: { confirmed: true, text: null },
    });
  });

  it("resolves a declined card from a later task's echo", () => {
    const resolved = resolveHitlAcrossTasks(cardOnlyMessages("adk-y"), [
      echoTask("adk-y", false),
    ]);
    const conf = resolved
      .flatMap((m) => m.segments)
      .find((s) => s.kind === "confirmation");
    expect(conf).toMatchObject({ answer: { confirmed: false, text: null } });
  });

  it("leaves an unanswered card untouched when no echo task exists", () => {
    const input = cardOnlyMessages("adk-z");
    const resolved = resolveHitlAcrossTasks(input, []);
    expect(resolved).toBe(input);
    const conf = resolved
      .flatMap((m) => m.segments)
      .find((s) => s.kind === "confirmation");
    expect(conf && "answer" in conf ? conf.answer : undefined).toBeUndefined();
  });

  it("resolves only the card whose callId matches, leaving others pending", () => {
    const messages: ChatMessage[] = [
      {
        id: "m1",
        role: "assistant",
        createdAt: 1,
        taskId: "task-card",
        segments: [
          {
            kind: "confirmation",
            callId: "adk-a",
            hint: "A?",
            originalCall: { name: "terminal", args: null },
            payload: null,
          },
          {
            kind: "confirmation",
            callId: "adk-b",
            hint: "B?",
            originalCall: { name: "terminal", args: null },
            payload: null,
          },
        ],
      },
    ];
    const resolved = resolveHitlAcrossTasks(messages, [
      echoTask("adk-a", true),
    ]);
    const byId = Object.fromEntries(
      resolved
        .flatMap((m) => m.segments)
        .filter((s) => s.kind === "confirmation")
        .map((s) => [s.callId, s.answer]),
    );
    expect(byId["adk-a"]).toEqual({ confirmed: true, text: null });
    expect(byId["adk-b"]).toBeUndefined();
  });

  it("carries the chosen answer text from a clarify echo payload", () => {
    const messages: ChatMessage[] = [
      {
        id: "m1",
        role: "assistant",
        createdAt: 1,
        taskId: "task-card",
        segments: [
          {
            kind: "clarify",
            callId: "adk-c",
            question: "Which?",
            choices: ["x", "y"],
          },
        ],
      },
    ];
    const answerTask = makeTask([
      msg({
        messageId: "echo",
        role: "user",
        parts: [
          dp(
            {
              id: "adk-c",
              name: "adk_request_confirmation",
              response: { confirmed: true, payload: { choice: "y" } },
            },
            { adk_type: "function_response" },
          ),
        ],
      }),
    ]);

    const clar = resolveHitlAcrossTasks(messages, [answerTask])
      .flatMap((m) => m.segments)
      .find((s) => s.kind === "clarify");
    expect(clar).toMatchObject({ answer: { confirmed: true, text: "y" } });
  });
});

describe("RESIDUAL: HITL-gated tool result attaches across echo / task boundary", () => {
  const call = (id: string, command: string) =>
    dp(
      { id, name: "terminal", args: { command } },
      { adk_type: "function_call" },
    );
  const card = (confId: string, origId: string) =>
    dp(
      {
        id: confId,
        name: "adk_request_confirmation",
        args: {
          originalFunctionCall: {
            id: origId,
            name: "terminal",
            args: { command: "rm -rf x" },
          },
          toolConfirmation: { hint: "Run rm -rf?", payload: null },
        },
      },
      { adk_type: "function_call" },
    );
  const echo = (confId: string) =>
    dp(
      {
        id: confId,
        name: "adk_request_confirmation",
        response: { confirmed: true, payload: null },
      },
      { adk_type: "function_response" },
    );
  const resp = (id: string) =>
    dp(
      { id, name: "terminal", response: { stdout: "done", exit_code: 0 } },
      { adk_type: "function_response" },
    );

  it("attaches the result when the response follows a user approve echo (same task)", () => {
    const task = makeTask([
      msg({ messageId: "u", role: "user", parts: [tp("run it")] }),
      msg({
        messageId: "call",
        role: "agent",
        parts: [call("c1", "rm -rf x")],
      }),
      msg({ messageId: "card", role: "agent", parts: [card("conf1", "c1")] }),
      msg({ messageId: "echo", role: "user", parts: [echo("conf1")] }),
      msg({ messageId: "resp", role: "agent", parts: [resp("c1")] }),
    ]);

    const messages = taskToChatMessages({ task, taskIndex: 0 });
    const tool = messages
      .flatMap((m) => m.segments)
      .find((s) => s.kind === "tool" && s.name === "terminal");
    expect(tool).toMatchObject({
      hasResult: true,
      result: { stdout: "done", exit_code: 0 },
    });
  });

  it("attaches the result when the response lands in a LATER task", () => {
    const taskA = {
      id: "tA",
      contextId: "ctx",
      status: { state: "completed" },
      history: [
        {
          messageId: "call",
          role: "agent",
          parts: [call("c1", "rm -rf x"), card("conf1", "c1")],
        },
      ],
    } as unknown as Task;
    const taskB = {
      id: "tB",
      contextId: "ctx",
      status: { state: "completed" },
      history: [
        { messageId: "echo", role: "user", parts: [echo("conf1")] },
        { messageId: "resp", role: "agent", parts: [resp("c1")] },
      ],
    } as unknown as Task;

    const a = taskToChatMessages({ task: taskA, taskIndex: 0 });
    const b = taskToChatMessages({ task: taskB, taskIndex: 1 });
    const combined = resolveToolResultsAcrossTasks(
      [...a, ...b],
      [taskA, taskB],
    );
    const tool = combined
      .flatMap((m) => m.segments)
      .find((s) => s.kind === "tool" && s.name === "terminal");
    expect(tool).toMatchObject({
      hasResult: true,
      result: { stdout: "done", exit_code: 0 },
    });
  });
});
