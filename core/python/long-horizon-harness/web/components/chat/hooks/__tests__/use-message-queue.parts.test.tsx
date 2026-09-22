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

import { renderHook, act } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { useMessageQueue } from "../use-message-queue";
import { makeDataPart } from "@/lib/a2a-part";

const part = () => makeDataPart({ lha_kind: "lha.selection" });

describe("queueing a message typed during a turn", () => {
  it("flushes its parts, not only its text", () => {
    // A queued "shorten this" without its anchor reaches the model with no
    // idea which span "this" is.
    const send = vi.fn();
    const { result, rerender } = renderHook(
      ({ busy }) => useMessageQueue({ busy, send }),
      { initialProps: { busy: true } },
    );

    act(() => result.current.enqueue("shorten this", [part()]));
    rerender({ busy: false });

    expect(send).toHaveBeenCalledTimes(1);
    const [text, opts] = send.mock.calls[0]!;
    expect(text).toBe("shorten this");
    expect(opts?.extraParts).toHaveLength(1);
  });

  it("accepts a message that is only parts", () => {
    // Dragging a file in and pressing send, with nothing typed.
    const send = vi.fn();
    const { result, rerender } = renderHook(
      ({ busy }) => useMessageQueue({ busy, send }),
      { initialProps: { busy: true } },
    );

    act(() => result.current.enqueue("", [part()]));
    expect(result.current.queue).toHaveLength(1);

    rerender({ busy: false });
    expect(send).toHaveBeenCalledTimes(1);
    expect(send.mock.calls[0]![1]?.extraParts).toHaveLength(1);
  });

  it("still ignores a wholly empty message", () => {
    const send = vi.fn();
    const { result } = renderHook(() => useMessageQueue({ busy: true, send }));
    act(() => result.current.enqueue("   "));
    expect(result.current.queue).toHaveLength(0);
  });

  it("carries parts from several queued messages", () => {
    const send = vi.fn();
    const { result, rerender } = renderHook(
      ({ busy }) => useMessageQueue({ busy, send }),
      { initialProps: { busy: true } },
    );

    act(() => result.current.enqueue("first", [part()]));
    act(() => result.current.enqueue("second", [part()]));
    rerender({ busy: false });

    const [text, opts] = send.mock.calls[0]!;
    expect(text).toBe("first\n\nsecond");
    expect(opts?.extraParts).toHaveLength(2);
  });

  it("sends no extraParts when nothing was attached", () => {
    const send = vi.fn();
    const { result, rerender } = renderHook(
      ({ busy }) => useMessageQueue({ busy, send }),
      { initialProps: { busy: true } },
    );
    act(() => result.current.enqueue("plain"));
    rerender({ busy: false });
    expect(send).toHaveBeenCalledWith("plain", undefined);
  });
});
