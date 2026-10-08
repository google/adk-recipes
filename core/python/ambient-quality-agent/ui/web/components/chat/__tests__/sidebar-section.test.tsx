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

// biome-ignore-all lint/style/noNonNullAssertion: a missing value fails the test either way; the assertion only narrows the type.

/**
 * The sidebar stacks these sections in a fixed-height flex column. Flex items
 * default to `min-height: auto`, so a section that does not opt out is exactly
 * as tall as its content and the leftover rail height becomes a blank gap
 * above the footer. These pin the class contract that lets sections share the
 * height instead: every open section takes an equal share (`flex-1`), capped
 * at its own content (`max-h-max`), with a floor of a header and about two
 * rows (`min-h-[5.5rem]`, clipped by `overflow-hidden`) so sections never
 * overlap when the rail runs out of height; a collapsed one is header-only and
 * out of the split. The header is a heading holding the toggle, and its
 * actions sit beside the toggle rather than inside it.
 */
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MessageSquare } from "lucide-react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { SidebarSection } from "../sidebar-section";

beforeEach(() => window.localStorage.clear());

const body = () => screen.getByTestId("body").parentElement!;
const section = () => body().parentElement!;

describe("SidebarSection", () => {
  it("open: takes an equal share of the rail, capped at its content, above a floor", () => {
    render(
      <SidebarSection title="Rail" icon={MessageSquare} storageKey="t.rail">
        <div data-testid="body" />
      </SidebarSection>,
    );

    expect(section().className).toContain("flex-1");
    expect(section().className).toContain("max-h-max");
    expect(section().className).toContain("min-h-[5.5rem]");
    expect(section().className).toContain("overflow-hidden");
    expect(body().className).toContain("min-h-0");
    expect(body().className).toContain("flex-1");
  });

  it("takes a caller's floor in place of the default", () => {
    render(
      <SidebarSection
        title="Rail"
        icon={MessageSquare}
        storageKey="t.rail"
        floorClassName="min-h-[8rem]"
      >
        <div data-testid="body" />
      </SidebarSection>,
    );

    expect(section().className).toContain("min-h-[8rem]");
    expect(section().className).not.toContain("min-h-[5.5rem]");
  });

  it("open with nothing to list: as tall as its content, out of the split", () => {
    render(
      <SidebarSection
        title="Rail"
        icon={MessageSquare}
        storageKey="t.rail"
        fill={false}
      >
        <div data-testid="body" />
      </SidebarSection>,
    );

    expect(section().className).toContain("shrink-0");
    expect(section().className).not.toContain("flex-1");
    expect(section().className).not.toContain("min-h-[5.5rem]");
    expect(screen.getByTestId("body")).toBeInTheDocument();
  });

  it("collapsed: header only, out of the split", async () => {
    render(
      <SidebarSection title="Chats" icon={MessageSquare} storageKey="t.chats">
        <div data-testid="body" />
      </SidebarSection>,
    );
    const el = section();

    await userEvent.click(screen.getByRole("button", { name: /Chats/ }));

    expect(screen.queryByTestId("body")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Chats/ })).toHaveAttribute(
      "aria-expanded",
      "false",
    );
    expect(el.className).toContain("shrink-0");
    expect(el.className).not.toContain("flex-1");
    expect(el.className).not.toContain("max-h-max");
    expect(el.className).not.toContain("min-h-[5.5rem]");
  });

  it("is a named region whose heading holds the toggle for its body", () => {
    render(
      <SidebarSection
        title="Top insights"
        icon={MessageSquare}
        storageKey="t.issues"
      >
        <div data-testid="body" />
      </SidebarSection>,
    );

    const heading = screen.getByRole("heading", {
      level: 2,
      name: "Top insights",
    });
    const toggle = screen.getByRole("button", { name: "Top insights" });
    expect(heading).toContainElement(toggle);
    expect(toggle).toHaveAttribute("aria-controls", body().id);
    expect(screen.getByRole("region", { name: "Top insights" })).toBe(
      section(),
    );
  });

  it("keeps header actions beside the toggle, not inside it", () => {
    render(
      <SidebarSection
        title="Top insights"
        icon={MessageSquare}
        storageKey="t.issues"
        rightSlot={<a href="/insights">All</a>}
      >
        <div data-testid="body" />
      </SidebarSection>,
    );

    const toggle = screen.getByRole("button", { name: "Top insights" });
    expect(toggle).not.toContainElement(
      screen.getByRole("link", { name: "All" }),
    );
  });

  it("shows the count as a pill, and no pill at zero or unknown", () => {
    const { rerender } = render(
      <SidebarSection
        title="Chats"
        icon={MessageSquare}
        storageKey="t.c"
        count={8}
      >
        <div />
      </SidebarSection>,
    );
    expect(screen.getByRole("button", { name: "Chats 8" })).toBeInTheDocument();

    rerender(
      <SidebarSection
        title="Chats"
        icon={MessageSquare}
        storageKey="t.c"
        count={0}
      >
        <div />
      </SidebarSection>,
    );
    expect(screen.getByRole("button", { name: "Chats" })).toBeInTheDocument();

    rerender(
      <SidebarSection title="Chats" icon={MessageSquare} storageKey="t.c">
        <div />
      </SidebarSection>,
    );
    expect(screen.getByRole("button", { name: "Chats" })).toBeInTheDocument();
  });
});

describe("SidebarSection without localStorage", () => {
  // A browser that denies storage access throws on the property read itself,
  // not only on setItem; the section must still render and toggle.
  it("renders and toggles when localStorage throws", async () => {
    const denied = () => {
      throw new DOMException("Access is denied", "SecurityError");
    };
    const spy = vi
      .spyOn(window, "localStorage", "get")
      .mockImplementation(denied);
    try {
      render(
        <SidebarSection
          title="Chats"
          icon={MessageSquare}
          storageKey="t.denied"
        >
          <div data-testid="body" />
        </SidebarSection>,
      );
      expect(screen.getByTestId("body")).toBeInTheDocument();

      await userEvent.click(screen.getByRole("button", { name: /Chats/ }));

      expect(screen.queryByTestId("body")).not.toBeInTheDocument();
    } finally {
      spy.mockRestore();
    }
  });
});
