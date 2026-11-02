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

import { beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

vi.mock("@/lib/horizon-sessions", () => ({
  renameLhaSession: vi.fn(async () => {}),
  deleteLhaSession: vi.fn(async () => {}),
}));

import { renameLhaSession } from "@/lib/horizon-sessions";
import { NowProvider } from "@/lib/now-context";
import { textStyle } from "@/lib/typography";
import { ChatRow } from "@/components/chat/chat-row";

function renderRow(
  title = "Checkout failures",
  onSelect = vi.fn(),
  active = false,
) {
  render(
    <NowProvider>
      <ul>
        <ChatRow
          session={{
            id: "c1",
            title,
            createdAt: Date.now(),
            lastUpdated: Date.now(),
            source: "user",
            jobType: null,
            workspaceWindow: [],
          }}
          active={active}
          onSelect={onSelect}
          onAfterRename={() => {}}
          onAfterDelete={() => {}}
        />
      </ul>
    </NowProvider>,
  );
  return onSelect;
}

describe("ChatRow keyboard", () => {
  beforeEach(() => vi.mocked(renameLhaSession).mockClear());

  it.each(["{Enter}", " "])(
    "pressing %j on Rename starts renaming instead of opening the chat",
    async (key) => {
      const user = userEvent.setup();
      const onSelect = renderRow();
      screen.getByRole("button", { name: "Rename chat" }).focus();
      await user.keyboard(key);
      expect(onSelect).not.toHaveBeenCalled();
      expect(screen.getByRole("textbox")).toBeInTheDocument();
    },
  );

  it.each(["{Enter}", " "])(
    "pressing %j on Delete asks for confirmation instead of opening the chat",
    async (key) => {
      const user = userEvent.setup();
      const onSelect = renderRow();
      screen.getByRole("button", { name: "Delete chat" }).focus();
      await user.keyboard(key);
      expect(onSelect).not.toHaveBeenCalled();
      expect(await screen.findByRole("alertdialog")).toBeInTheDocument();
    },
  );

  it("keeps spaces typed into the rename input", async () => {
    const user = userEvent.setup();
    renderRow();
    screen.getByRole("button", { name: "Rename chat" }).focus();
    await user.keyboard("{Enter}");
    const input = screen.getByRole("textbox");
    await user.clear(input);
    await user.type(input, "hello big world{Enter}");
    await waitFor(() =>
      expect(renameLhaSession).toHaveBeenCalledWith("c1", "hello big world"),
    );
  });

  it("returns focus to the row when a keyboard rename ends", async () => {
    const user = userEvent.setup();
    renderRow();
    screen.getByRole("button", { name: "Rename chat" }).focus();
    await user.keyboard("{Enter}");
    await waitFor(() => expect(screen.getByRole("textbox")).toHaveFocus());
    await user.keyboard("{Escape}");
    expect(
      screen.getByRole("button", { name: /Checkout failures/ }),
    ).toHaveFocus();
  });

  it("opens the chat on Enter from the row itself", async () => {
    const user = userEvent.setup();
    const onSelect = renderRow();
    screen.getByRole("button", { name: /Checkout failures/ }).focus();
    await user.keyboard("{Enter}");
    expect(onSelect).toHaveBeenCalledWith("c1");
  });
});

describe("ChatRow structure", () => {
  it("keeps the action buttons outside the row's own button", () => {
    renderRow();
    const row = screen.getByRole("button", { name: /Checkout failures/ });
    expect(row.tagName).toBe("BUTTON");
    expect(row.querySelector("button, [role=button]")).toBeNull();
  });

  it("gives the action buttons a 24px target", () => {
    renderRow();
    for (const name of ["Rename chat", "Delete chat"]) {
      const cls = screen.getByRole("button", { name }).className;
      expect(cls).toContain("h-6");
      expect(cls).toContain("w-6");
    }
  });

  it("lets the browser pick the title's direction so RTL truncates at its end", () => {
    renderRow("يستدعي الوكيل أداة تقديم المصروفات");
    expect(
      screen.getByText("يستدعي الوكيل أداة تقديم المصروفات"),
    ).toHaveAttribute("dir", "auto");
  });

  it.each(["", "   "])("labels a blank title %j as Untitled chat", (title) => {
    renderRow(title);
    expect(screen.getByText("Untitled chat")).toBeInTheDocument();
  });
});

describe("ChatRow visuals", () => {
  it("marks the active row with the accent background alone, no side bar", () => {
    renderRow("Checkout failures", vi.fn(), true);
    const row = screen.getByRole("button", { name: /Checkout failures/ });
    expect(row.className).toContain("bg-accent");
    expect(row.className).not.toContain("shadow-");
    expect(row.className).not.toContain("before:");
  });

  it("marks only the active row as the current page, so the rail can scroll it into view", () => {
    renderRow("Checkout failures", vi.fn(), true);
    expect(
      screen.getByRole("button", { name: /Checkout failures/ }),
    ).toHaveAttribute("aria-current", "page");
  });

  it("leaves an inactive row without aria-current", () => {
    renderRow();
    expect(
      screen.getByRole("button", { name: /Checkout failures/ }),
    ).not.toHaveAttribute("aria-current");
  });

  // Tabular figures come from body, so the stamp only needs the meta role.
  it("draws the timestamp as meta, at full muted contrast", () => {
    renderRow();
    const stamp = screen.getByText("now");
    expect(stamp.className.split(" ")).toEqual(
      expect.arrayContaining(textStyle.meta.split(" ")),
    );
    expect(stamp.className).not.toMatch(/text-muted-foreground\//);
  });
});

describe("ChatRow rename length cap", () => {
  beforeEach(() => vi.mocked(renameLhaSession).mockClear());

  it("never saves half an emoji at the 80-unit limit", async () => {
    const user = userEvent.setup();
    const family = "👨‍👩‍👧‍👦"; // 11 UTF-16 units
    renderRow();
    screen.getByRole("button", { name: "Rename chat" }).focus();
    await user.keyboard("{Enter}");
    const input = screen.getByRole("textbox");
    await user.clear(input);
    await user.click(input);
    await user.paste(family.repeat(12));
    await user.keyboard("{Enter}");
    await waitFor(() => expect(renameLhaSession).toHaveBeenCalled());
    const saved = vi.mocked(renameLhaSession).mock.calls[0][1];
    expect(saved).toBe(family.repeat(7));
  });

  it("refuses a keystroke into a full title instead of dropping its end", async () => {
    const user = userEvent.setup();
    const full = "0123456789".repeat(8);
    renderRow(full);
    screen.getByRole("button", { name: "Rename chat" }).focus();
    await user.keyboard("{Enter}");
    const input = screen.getByRole("textbox") as HTMLInputElement;
    input.setSelectionRange(10, 10);

    await user.keyboard("X");

    expect(input.value).toBe(full);
    // The caret stays where the reader was typing.
    expect(input.selectionStart).toBe(10);
  });
});
