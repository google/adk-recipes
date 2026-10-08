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

import { createContext, useContext, type ReactNode } from "react";

// Read inside UserTextSegment to disable resend/edit while a stream is
// in-flight. Lifting this out of MessageBubble props means a busy toggle no
// longer re-renders every message in the list — only the buttons that
// actually care.
const BusyContext = createContext<boolean>(false);

export function BusyProvider({
  value,
  children,
}: {
  value: boolean;
  children: ReactNode;
}) {
  return <BusyContext.Provider value={value}>{children}</BusyContext.Provider>;
}

export function useBusy(): boolean {
  return useContext(BusyContext);
}
