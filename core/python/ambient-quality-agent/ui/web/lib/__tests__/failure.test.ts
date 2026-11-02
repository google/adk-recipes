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
import { HttpError, ReadTimeoutError } from "../aqua-api";
import { describeFailure, isNoData } from "../failure";

/** The message b/563290003 was filed on, verbatim apart from the truncated
 *  STRUCT — a deployment whose export table has never carried the column the
 *  sweep's query names. */
const MISSING_COLUMN =
  "400 Field name service_version does not exist in STRUCT<" +
  "gen_ai_conversation_id STRING, gen_ai_agent_name STRING, ...> at [28:24]; " +
  "reason: invalidQuery, location: query Location: us-east1 " +
  "Job ID: ec82f7d4-4840-4792-b96e-986cafe20481";

describe("describeFailure", () => {
  it("reads a column BigQuery has not inferred yet as an empty deployment", () => {
    const failure = describeFailure(new Error(MISSING_COLUMN));

    expect(failure.kind).toBe("no-data");
    expect(failure.cause).toBe("No telemetry to analyze yet.");
    // The column is named, so whoever set the sink up knows which signal is
    // missing without opening the detail.
    expect(failure.hint).toContain("service_version");
    expect(failure.hint).toContain(
      "once the observed agent has served traffic",
    );
    // And the raw text survives for whoever has to debug the sink.
    expect(failure.detail).toBe(MISSING_COLUMN);
  });

  it("reads a table the sink has not created yet as an empty deployment", () => {
    for (const message of [
      "404 Not found: Table my-project:aqua.spans was not found in location US",
      "NotFound: 404 Not found: Dataset my-project:aqua",
    ]) {
      const failure = describeFailure(new Error(message));
      expect(failure.kind).toBe("no-data");
      expect(failure.cause).toBe("No telemetry to analyze yet.");
      expect(failure.hint).toContain(
        "once the observed agent has served traffic",
      );
      expect(failure.detail).toBe(message);
    }
  });

  it("reads an engine with no agent on it as a state, not a fault", () => {
    // Verbatim from `ui/app.py`'s `_NOT_DEPLOYED`, which that module's tests
    // pin against the pattern this one exercises.
    const failure = describeFailure(
      new Error(
        "AQuA's agent is not serving its API yet. The Agent Runtime agent is " +
          "still running the placeholder image Terraform creates it with, or " +
          "is rolling out a new revision; either takes several minutes.",
      ),
    );

    expect(failure.kind).toBe("pending");
    // The first sentence alone, so a compact pane shows the state without the
    // paragraph behind it.
    expect(failure.cause).toBe("AQuA's agent is not serving its API yet.");
    expect(failure.hint).toContain("placeholder image");
    expect(failure.detail).toBe("");
    // Not an empty deployment: there is nothing to say has no data yet.
    expect(isNoData(failure.cause)).toBe(false);
  });

  it("keeps an unreachable agent and a timeout as failures, and apart", () => {
    // A timeout is worth retrying as-is; an unreachable agent is worth
    // checking the deployment for. Collapsing them throws that away.
    expect(describeFailure(new ReadTimeoutError("/api/stats"))).toMatchObject({
      kind: "error",
      cause: "the agent didn't respond in time.",
    });
    expect(
      describeFailure(new HttpError("/api/stats -> 502", 502)),
    ).toMatchObject({
      kind: "error",
      cause: "couldn't reach the agent.",
    });
    expect(describeFailure(new TypeError("Failed to fetch"))).toMatchObject({
      kind: "error",
      cause: "couldn't reach the agent.",
    });
  });

  it("passes an unrecognized fault through as its own message", () => {
    const failure = describeFailure(
      new Error("permission denied on aqua.insights"),
    );
    expect(failure.kind).toBe("error");
    expect(failure.cause).toBe("permission denied on aqua.insights");
    // Already in `cause`; a disclosure repeating it would be noise.
    expect(failure.detail).toBe("");
  });

  it("does not mistake a genuine BigQuery fault for emptiness", () => {
    // Close to the no-data signatures in wording, and neither is one: the
    // classifier keys on the sink's own two shapes, not on "not found".
    for (const message of [
      "400 Syntax error: Unexpected keyword STRUCT at [3:1]",
      "404 Not found: Job my-project:us-east1.ec82f7d4",
      "403 Access Denied: Table my-project:aqua.spans",
    ]) {
      expect(describeFailure(new Error(message)).kind).toBe("error");
    }
  });

  it("classifies a plain string, which is how a run's error arrives", () => {
    // `Run.error` is a JSON string off the wire, not an `Error`.
    expect(isNoData(MISSING_COLUMN)).toBe(true);
    expect(isNoData("the model refused")).toBe(false);
    expect(isNoData(null)).toBe(false);
  });

  it("says something when there is no message at all", () => {
    const failure = describeFailure(new Error(""));
    expect(failure.kind).toBe("error");
    expect(failure.cause).toBe("no reason given.");
  });
});
