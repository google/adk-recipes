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

/**
 * Every route renders its own rail, so a sidebar list remounts on each
 * navigation. These pin that it comes back at the offset it was left at, that
 * the current route's row is then in view, and that `data-more` says whether
 * rows remain below the fold. jsdom has no layout, so the geometry is stubbed.
 */
import { fireEvent, render, screen } from "@testing-library/react";
import { useState } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { resetRailScrollForTest, useRailScroll } from "../use-rail-scroll";

function List({ activeIndex }: { activeIndex?: number }) {
  const ref = useRailScroll<HTMLDivElement>("t.list");
  return <Rows scrollRef={ref} activeIndex={activeIndex} />;
}

function Rows({
  scrollRef,
  activeIndex,
  nested = false,
}: {
  scrollRef: React.Ref<HTMLDivElement>;
  activeIndex?: number;
  nested?: boolean;
}) {
  return (
    <div ref={scrollRef} data-testid="list">
      {Array.from({ length: 20 }, (_, i) => {
        const current = i === activeIndex ? "page" : undefined;
        // biome-ignore-start lint/suspicious/noArrayIndexKey: the rows are generated from their index, which is their only identity.
        return nested ? (
          <li key={i} data-index={i}>
            <button type="button" aria-current={current}>
              row {i}
            </button>
          </li>
        ) : (
          <a key={i} href={`#row-${i}`} aria-current={current} data-index={i}>
            row {i}
          </a>
        );
        // biome-ignore-end lint/suspicious/noArrayIndexKey: the rows are generated from their index, which is their only identity.
      })}
    </div>
  );
}

/** 20 rows of 30px in a 100px viewport. A row's box is its own, or its
 *  enclosing row's for an element inside it. */
function stubGeometry() {
  vi.spyOn(HTMLElement.prototype, "clientHeight", "get").mockReturnValue(100);
  vi.spyOn(HTMLElement.prototype, "scrollHeight", "get").mockReturnValue(600);
  vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockImplementation(
    function (this: HTMLElement) {
      const list = this.closest<HTMLElement>('[data-testid="list"]');
      const row = this.closest<HTMLElement>("[data-index]");
      if (!list || !row) return { top: 0, bottom: 100, height: 100 } as DOMRect;
      const top = Number(row.dataset.index) * 30 - list.scrollTop;
      return { top, bottom: top + 30, height: 30 } as DOMRect;
    },
  );
}

beforeEach(() => {
  resetRailScrollForTest();
  stubGeometry();
});
afterEach(() => vi.restoreAllMocks());

describe("useRailScroll", () => {
  it("restores the offset a remounted list was left at", () => {
    const first = render(<List />);
    const list = screen.getByTestId("list");
    list.scrollTop = 240;
    fireEvent.scroll(list);
    first.unmount();

    render(<List />);

    expect(screen.getByTestId("list").scrollTop).toBe(240);
  });

  it("brings the current route's row into view", () => {
    render(<List activeIndex={10} />);

    // Row 10 spans 300-330px; centred in the 100px viewport.
    expect(screen.getByTestId("list").scrollTop).toBe(265);
  });

  it("brings the current row into view when it sits inside a positioned row", () => {
    // A chat row's aria-current is on a button inside a `relative` li, so its
    // offsetTop is measured from the li rather than from the list.
    function NestedList() {
      const ref = useRailScroll<HTMLDivElement>("t.nested");
      return <Rows scrollRef={ref} activeIndex={10} nested />;
    }
    render(<NestedList />);

    expect(screen.getByTestId("list").scrollTop).toBe(265);
  });

  it("marks the list while rows remain below the fold, not at the end", () => {
    render(<List />);
    const list = screen.getByTestId("list");
    expect(list.dataset.more).toBe("true");

    list.scrollTop = 500;
    fireEvent.scroll(list);

    expect(list.dataset.more).toBe("false");
  });

  it("keeps working on a list that a collapsed section brings back", () => {
    function Section() {
      const ref = useRailScroll<HTMLDivElement>("t.section");
      const [open, setOpen] = useState(true);
      return (
        <>
          <button type="button" onClick={() => setOpen((o) => !o)}>
            toggle
          </button>
          {open && <Rows scrollRef={ref} activeIndex={10} />}
        </>
      );
    }
    render(<Section />);
    fireEvent.click(screen.getByRole("button", { name: "toggle" }));
    fireEvent.click(screen.getByRole("button", { name: "toggle" }));

    const list = screen.getByTestId("list");
    expect(list.scrollTop).toBe(265);
    expect(list.dataset.more).toBe("true");
    list.scrollTop = 500;
    fireEvent.scroll(list);
    expect(list.dataset.more).toBe("false");
  });
});
