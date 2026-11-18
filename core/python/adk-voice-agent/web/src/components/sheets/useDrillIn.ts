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

import { useCallback, useLayoutEffect, useRef, useState } from "react";

/** Stack navigation inside a sheet; restores each parent page's scroll offset on `pop`. */
export function useDrillIn<Page>(root: Page, start?: Page) {
  const [stack, setStack] = useState<Page[]>(() =>
    start === undefined ? [root] : [root, start],
  );
  // Animate only after the first move; the sheet opening is its own motion.
  const [direction, setDirection] = useState<"push" | "pop">();
  const viewRef = useRef<HTMLDivElement>(null);
  const scrolls = useRef<number[]>([]);

  const push = useCallback((page: Page) => {
    scrolls.current.push(
      viewRef.current?.closest(".sheet-body")?.scrollTop ?? 0,
    );
    setDirection("push");
    setStack((s) => [...s, page]);
  }, []);

  const pop = useCallback(() => {
    setDirection("pop");
    setStack((s) => (s.length > 1 ? s.slice(0, -1) : s));
  }, []);

  useLayoutEffect(() => {
    if (!direction || stack.length === 0) return;
    const top = direction === "pop" ? (scrolls.current.pop() ?? 0) : 0;
    viewRef.current?.closest(".sheet-body")?.scrollTo?.({ top });
  }, [stack.length, direction]);

  return {
    page: stack[stack.length - 1],
    parent: stack.length > 1 ? stack[stack.length - 2] : undefined,
    depth: stack.length,
    push,
    pop,
    viewRef,
    direction,
  };
}
