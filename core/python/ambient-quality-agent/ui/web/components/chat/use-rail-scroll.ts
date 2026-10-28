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

import { useEffect, useLayoutEffect, useState } from "react";

/** Scroll behaviour shared by the sidebar's lists: Chats, Top insights and
 *  Recent investigations.
 *
 * Every route renders its own rail, so each navigation remounts these lists.
 * Their scroll offsets live here, at module scope, so a list comes back where
 * the reader left it; and the row for the current route is then brought into
 * view, since that is the row a reader who just clicked expects to see.
 *
 * The hook also keeps `data-more` on the list: "true" while rows remain below
 * the fold. `RAIL_SCROLL_CLASS` fades the bottom edge on it, so a list cut
 * mid-row reads as "more below" rather than as a rendering fault.
 */

const offsets = new Map<string, number>();

/** Height of the bottom fade in `RAIL_SCROLL_CLASS`, 1.5rem at the default
 *  text size. */
const FADE_PX = 24;

/** Forgets every remembered offset. For tests, which share the module. */
export function resetRailScrollForTest(): void {
  offsets.clear();
}

/** Classes for the element `useRailScroll`'s ref is attached to, horizontal
 *  padding included. The scrollbar's gutter is always reserved, so rows keep
 *  their width (and wrap the same) when a list starts or stops overflowing;
 *  the right padding is the rail's 8px less the 6px scrollbar from
 *  globals.css. The fade is off under forced colors, where a mask would erase
 *  text the user asked to see at full contrast. */
export const RAIL_SCROLL_CLASS =
  "min-h-0 flex-1 overflow-y-auto pl-2 pr-0.5 [scrollbar-gutter:stable] " +
  "[@media(forced-colors:none)]:data-[more=true]:[mask-image:linear-gradient(to_bottom,#000_calc(100%-1.5rem),transparent)]";

/** Remembers the list's scroll offset under `key`, restores it on each element
 *  the returned ref is attached to, brings the `aria-current="page"` row into
 *  view, and keeps `data-more` current. Attach the ref to the scrolling
 *  element, which must hold its rows when it mounts.
 *
 *  The ref is a callback rather than a ref object because the element can be
 *  replaced while the caller stays mounted: Chats' list unmounts when its
 *  section collapses and a new one mounts when it expands. */
export function useRailScroll<T extends HTMLElement>(key: string) {
  const [el, setEl] = useState<T | null>(null);

  useLayoutEffect(() => {
    if (!el) return;
    el.scrollTop = offsets.get(key) ?? 0;
    const active = el.querySelector<HTMLElement>('[aria-current="page"]');
    if (active) {
      // Measured against the list rather than with offsetTop, which counts
      // from the nearest positioned ancestor: for a chat that is its row.
      const box = active.getBoundingClientRect();
      const top = box.top - el.getBoundingClientRect().top + el.scrollTop;
      // Short of the bottom fade, and centred rather than flush with an edge,
      // where a sticky label or the fade would cover it. Not scrollIntoView,
      // which also scrolls the rail around the list when the list is below
      // its fold.
      if (
        top < el.scrollTop ||
        top + box.height > el.scrollTop + el.clientHeight - FADE_PX
      ) {
        el.scrollTop = top - (el.clientHeight - box.height) / 2;
      }
    }
  }, [el, key]);

  useEffect(() => {
    if (!el) return;
    const update = () => {
      el.dataset.more = String(
        el.scrollHeight - el.scrollTop - el.clientHeight > 1,
      );
    };
    const onScroll = () => {
      offsets.set(key, el.scrollTop);
      update();
    };
    update();
    el.addEventListener("scroll", onScroll, { passive: true });
    // The list's box changes when a sibling section collapses or the window
    // resizes; its content changes when rows arrive. Either can start or end
    // an overflow without a scroll event.
    const resize = new ResizeObserver(update);
    resize.observe(el);
    const mutation = new MutationObserver(update);
    mutation.observe(el, { childList: true, subtree: true });
    return () => {
      el.removeEventListener("scroll", onScroll);
      resize.disconnect();
      mutation.disconnect();
    };
  }, [el, key]);

  return setEl;
}
