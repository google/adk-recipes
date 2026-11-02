// Copyright 2026 Google LLC
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     https://www.apache.org/licenses/LICENSE-2.0
import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { Loading, LoadingPane } from "../loading";

describe("Loading", () => {
  it("spins, so a slow read looks busy rather than stalled", () => {
    const { container } = render(<Loading />);
    expect(container.querySelector(".animate-spin")).not.toBeNull();
  });

  it("announces itself to a screen reader", () => {
    render(<Loading />);
    expect(screen.getByRole("status")).toHaveTextContent("Loading");
  });

  it("takes a label so the pane says what it is waiting for", () => {
    render(<LoadingPane label="Loading insights…" />);
    expect(screen.getByRole("status")).toHaveTextContent("Loading insights…");
  });
});
