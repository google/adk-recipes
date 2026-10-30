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

import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import Markdown from "./Markdown";

describe("Markdown", () => {
  it("renders bold, italic and code without the markers", () => {
    const { container } = render(
      <Markdown text="Use **Photo Try-On**, *Video* or `spin`." />,
    );
    expect(screen.getByText("Photo Try-On").tagName).toBe("STRONG");
    expect(screen.getByText("Video").tagName).toBe("EM");
    expect(screen.getByText("spin").tagName).toBe("CODE");
    expect(container.textContent).toBe("Use Photo Try-On, Video or spin.");
  });

  it("groups bullet and numbered lines into lists, other lines into paragraphs", () => {
    const { container } = render(
      <Markdown
        text={"Pick one:\n- **Emma**\n- Luca\n\n1. Upload\n2. Generate\nDone."}
      />,
    );
    expect(container.querySelectorAll("ul > li")).toHaveLength(2);
    expect(container.querySelectorAll("ol > li")).toHaveLength(2);
    expect(container.querySelectorAll("p")).toHaveLength(2);
    expect(screen.getByText("Emma").tagName).toBe("STRONG");
  });

  it("keeps https links and shows other links as plain text", () => {
    render(
      <Markdown text="[docs](https://example.com/a) and [bad](javascript:alert(1))" />,
    );
    expect(
      screen.getByRole("link", { name: "docs" }).getAttribute("href"),
    ).toBe("https://example.com/a");
    expect(screen.queryByRole("link", { name: "bad" })).toBeNull();
  });

  it("shows the host of a link whose text is not its URL", () => {
    const { container } = render(
      <Markdown text="See [your bank](https://login.evil.example/x?y=1)." />,
    );
    const link = screen.getByRole("link", { name: "your bank" });
    expect(link.getAttribute("target")).toBe("_blank");
    expect(link.getAttribute("rel")).toBe("noreferrer");
    expect(container.textContent).toBe("See your bank (login.evil.example).");
  });

  it("shows no host when the link text is already the URL", () => {
    const { container } = render(
      <Markdown text="[https://example.com/a/](https://example.com/a) and [example.com/a](https://example.com/a)" />,
    );
    expect(screen.getAllByRole("link")).toHaveLength(2);
    expect(container.textContent).toBe(
      "https://example.com/a/ and example.com/a",
    );
  });

  it("never renders HTML from the text", () => {
    const { container } = render(
      <Markdown text={'<img src=x onerror="alert(1)"> **hi**'} />,
    );
    expect(container.querySelector("img")).toBeNull();
    expect(container.textContent).toContain("<img src=x");
  });
});
