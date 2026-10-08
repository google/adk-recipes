// Copyright 2026 Google LLC
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     https://www.apache.org/licenses/LICENSE-2.0
import { describe, expect, it } from "vitest";

import {
  conversationsSnapshot,
  rememberConversation,
  resetConversationsForTest,
} from "@/lib/aqua-conversations";

describe("conversationsSnapshot", () => {
  it("returns the same reference until the store changes", () => {
    // useSyncExternalStore compares by identity: a fresh array every call is
    // an infinite render loop, which is exactly how the sidebar rail crashed
    // the whole app with React error #185.
    resetConversationsForTest();
    rememberConversation("c1", "hello");
    const first = conversationsSnapshot();
    expect(conversationsSnapshot()).toBe(first);

    rememberConversation("c2", "another");
    const second = conversationsSnapshot();
    expect(second).not.toBe(first);
    expect(second.map((c) => c.id).sort()).toEqual(["c1", "c2"]);
  });
});
