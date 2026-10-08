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

import { describe, expect, it, vi } from "vitest";
import { act, renderHook } from "@testing-library/react";
import { useMessageQueue } from "@/components/chat/hooks/use-message-queue";

describe("useMessageQueue", () => {
  it("enqueues trimmed text and ignores blank entries", () => {
    const send = vi.fn();
    const { result } = renderHook(() => useMessageQueue({ busy: true, send }));
    act(() => result.current.enqueue("  hello  "));
    act(() => result.current.enqueue("   "));
    expect(result.current.queue).toEqual([{ text: "hello", parts: [] }]);
  });

  it("removes a queued entry by index", () => {
    const send = vi.fn();
    const { result } = renderHook(() => useMessageQueue({ busy: true, send }));
    act(() => result.current.enqueue("a"));
    act(() => result.current.enqueue("b"));
    act(() => result.current.removeQueued(0));
    expect(result.current.queue).toEqual([{ text: "b", parts: [] }]);
  });

  it("clears the queue", () => {
    const send = vi.fn();
    const { result } = renderHook(() => useMessageQueue({ busy: true, send }));
    act(() => result.current.enqueue("a"));
    act(() => result.current.clearQueue());
    expect(result.current.queue).toEqual([]);
  });

  it("flushes the combined queue as one send on busy->idle", () => {
    const send = vi.fn();
    const { result, rerender } = renderHook(
      ({ busy }) => useMessageQueue({ busy, send }),
      { initialProps: { busy: true } },
    );
    act(() => result.current.enqueue("a"));
    act(() => result.current.enqueue("b"));
    rerender({ busy: false });
    expect(send).toHaveBeenCalledTimes(1);
    expect(send).toHaveBeenCalledWith("a\n\nb", undefined);
    expect(result.current.queue).toEqual([]);
  });

  it("does not flush when the queue is empty", () => {
    const send = vi.fn();
    const { rerender } = renderHook(
      ({ busy }) => useMessageQueue({ busy, send }),
      { initialProps: { busy: true } },
    );
    rerender({ busy: false });
    expect(send).not.toHaveBeenCalled();
  });
});
