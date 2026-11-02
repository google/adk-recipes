/**
 * Copyright 2026 Google LLC
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     https://www.apache.org/licenses/LICENSE-2.0
 *
 * Unless required by applicable law or agreed to in writing, software
 * distributed under the License is distributed on an "AS IS" BASIS,
 * WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 * See the License for the specific language governing permissions and
 * limitations under the License.
 */

import { screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { renderAt } from "../test/utils";
import TopNav from "./TopNav";

describe("TopNav", () => {
  it("shows the brand linking to the Studio, with no page links or agent mode switch", () => {
    renderAt("/", { "/": <TopNav /> });

    const links = screen.getAllByRole("link");
    expect(links).toHaveLength(1);
    expect(links[0].getAttribute("href")).toBe("/");
    expect(screen.getByAltText("GenMedia for Commerce")).toBeTruthy();
    expect(screen.queryByRole("switch")).toBeNull();
    expect(screen.queryByRole("navigation")).toBeNull();
  });
});
