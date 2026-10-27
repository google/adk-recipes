// Copyright 2026 Google LLC
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     https://www.apache.org/licenses/LICENSE-2.0
import { describe, expect, it } from "vitest";

import { HttpError } from "../aqua-api";
import { makeQueryClient } from "../query-client";

function retryFor(error: Error, attempt = 0): boolean {
  const retry = makeQueryClient().getDefaultOptions().queries?.retry;
  if (typeof retry !== "function") throw new Error("retry is not a predicate");
  return retry(attempt, error) as boolean;
}

describe("query retry policy", () => {
  it("does not retry a 501, which is what the /lha stubs answer", () => {
    expect(retryFor(new HttpError("/lha/x -> 501", 501))).toBe(false);
  });

  it("does not retry a 404", () => {
    expect(retryFor(new HttpError("/api/x -> 404", 404))).toBe(false);
  });

  it("still retries a 500", () => {
    expect(retryFor(new HttpError("/api/x -> 500", 500))).toBe(true);
  });

  it("reads the status out of the message when there is no status field", () => {
    expect(retryFor(new Error("/lha/sessions -> 501"))).toBe(false);
  });
});
