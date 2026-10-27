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

import {
  createContext,
  useContext,
  useEffect,
  useState,
  type ReactNode,
} from "react";

// One ticking source for every "Xm ago" / "Xs" label in the tree. Without
// this, each MessageTimestamp / sidebar row span owned its own setInterval —
// N bubbles meant N timers waking the event loop.
const NowContext = createContext<number>(Date.now());

export interface NowProviderProps {
  children: ReactNode;
  intervalMs?: number;
}

export function NowProvider({
  children,
  intervalMs = 30_000,
}: NowProviderProps) {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const id = setInterval(() => setNow(Date.now()), intervalMs);
    return () => clearInterval(id);
  }, [intervalMs]);
  return <NowContext.Provider value={now}>{children}</NowContext.Provider>;
}

export function useNow(): number {
  return useContext(NowContext);
}
