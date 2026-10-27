// Copyright 2026 Google LLC
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     https://www.apache.org/licenses/LICENSE-2.0
import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { GcsLink, gcsConsoleUrl } from "../gcs-link";

describe("gcsConsoleUrl", () => {
  it("maps a gs:// object to its console page", () => {
    expect(gcsConsoleUrl("gs://my-bucket/current/goal.md")).toBe(
      "https://console.cloud.google.com/storage/browser/_details/my-bucket/current/goal.md",
    );
  });

  it("handles an object at the bucket root", () => {
    expect(gcsConsoleUrl("gs://jobs-bucket/memories.md")).toBe(
      "https://console.cloud.google.com/storage/browser/_details/jobs-bucket/memories.md",
    );
  });

  it("returns null for a bucket with no object, so no dead link renders", () => {
    expect(gcsConsoleUrl("gs://only-a-bucket")).toBeNull();
  });

  it("returns null when there is no uri at all", () => {
    expect(gcsConsoleUrl(undefined)).toBeNull();
    expect(gcsConsoleUrl(null)).toBeNull();
  });
});

describe("GcsLink", () => {
  it("renders nothing when the bucket is unconfigured", () => {
    const { container } = render(<GcsLink uri={null} />);
    expect(container.firstChild).toBeNull();
  });

  it("opens in a new tab and shows the full uri on hover", () => {
    render(<GcsLink uri="gs://b/current/goal.md" />);
    const link = screen.getByRole("link");
    expect(link).toHaveAttribute("target", "_blank");
    expect(link).toHaveAttribute("title", "gs://b/current/goal.md");
  });
});
