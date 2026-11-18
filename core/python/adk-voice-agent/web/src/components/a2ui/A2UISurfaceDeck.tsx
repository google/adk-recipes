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

import { ChevronLeft, ChevronRight, X } from "lucide-react";
import { type PointerEvent, useEffect, useRef, useState } from "react";
import type { A2UISurface } from "../../lib/api";
import { BrandIcon } from "../BrandIcons";
import { IconButton } from "../common/ui";
import { ContextSurfaceBody } from "./context/ContextSurfaceBody";

const SWIPE_PX = 48;
const MAX_DOTS = 7;

function ActiveSurfaceCard({
  surface,
  onDismiss,
}: {
  surface: A2UISurface;
  onDismiss: () => void;
}) {
  return (
    <article className="a2ui-card" data-testid="a2ui-surface-card">
      <header className="card-head">
        <span className="card-source">
          <BrandIcon icon={surface.harness || "google_search"} size={18} />
          <span className="card-source-label">{surface.title}</span>
        </span>
        <IconButton
          className="card-close"
          onClick={onDismiss}
          label="Dismiss"
          title="Dismiss (Esc)"
        >
          <X size={16} />
        </IconButton>
      </header>
      <ContextSurfaceBody surface={surface} />
    </article>
  );
}

/** Newly arrived cards take focus once `ready` is true so initial hydration doesn't jump. */
function useActiveSurface(surfaces: A2UISurface[], ready: boolean) {
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const seen = useRef<Set<string> | null>(null);

  useEffect(() => {
    if (!ready) return;
    const ids = surfaces.map((s) => s.surfaceId);
    if (seen.current) {
      const arrived = ids.find((id) => !seen.current?.has(id));
      if (arrived) setSelectedId(arrived);
    }
    seen.current = new Set(ids);
  }, [surfaces, ready]);

  const active =
    surfaces.find((s) => s.surfaceId === selectedId) ?? surfaces[0] ?? null;
  return { active, select: setSelectedId };
}

const isTyping = (target: EventTarget | null) =>
  target instanceof HTMLElement &&
  (target.isContentEditable ||
    ["INPUT", "TEXTAREA", "SELECT"].includes(target.tagName));

export function A2UISurfaceDeck({
  surfaces,
  ready,
  onDismissSurface,
}: {
  surfaces: A2UISurface[];
  ready: boolean;
  onDismissSurface: (surface: A2UISurface) => void;
}) {
  const { active, select } = useActiveSurface(surfaces, ready);
  const swipeStart = useRef<number | null>(null);

  const index = active
    ? surfaces.findIndex((s) => s.surfaceId === active.surfaceId)
    : -1;
  const page = (step: number) => {
    const next = surfaces[index + step];
    if (next) select(next.surfaceId);
  };
  const dismiss = () => active && onDismissSurface(active);
  const keys = useRef({ page, dismiss });
  keys.current = { page, dismiss };
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.defaultPrevented || e.metaKey || e.ctrlKey || e.altKey) return;
      if (isTyping(e.target) || document.querySelector(".sheet-backdrop"))
        return;
      if (e.key === "ArrowRight") keys.current.page(1);
      else if (e.key === "ArrowLeft") keys.current.page(-1);
      else if (e.key === "Escape") keys.current.dismiss();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  if (!active) return null;

  const onPointerUp = (e: PointerEvent) => {
    if (swipeStart.current === null) return;
    const dx = e.clientX - swipeStart.current;
    swipeStart.current = null;
    if (Math.abs(dx) > SWIPE_PX) page(dx < 0 ? 1 : -1);
  };

  return (
    <div className="deck">
      <div
        className="deck-slide"
        onPointerDown={(e) => {
          swipeStart.current = e.clientX;
        }}
        onPointerUp={onPointerUp}
        onPointerCancel={() => (swipeStart.current = null)}
      >
        <ActiveSurfaceCard
          key={active.surfaceId}
          surface={active}
          onDismiss={dismiss}
        />
      </div>
      {surfaces.length > 1 && (
        <nav className="deck-pager" aria-label="Cards">
          <IconButton
            className="deck-step"
            label="Previous card"
            title="Previous (←)"
            disabled={index <= 0}
            onClick={() => page(-1)}
          >
            <ChevronLeft size={16} />
          </IconButton>
          {surfaces.length > MAX_DOTS ? (
            <span className="deck-count">
              {index + 1} of {surfaces.length}
            </span>
          ) : (
            surfaces.map((s, i) => (
              <button
                key={s.surfaceId}
                type="button"
                className="deck-dot"
                aria-label={`${s.title} (${i + 1} of ${surfaces.length})`}
                aria-current={s.surfaceId === active.surfaceId}
                onClick={() => select(s.surfaceId)}
              />
            ))
          )}
          <IconButton
            className="deck-step"
            label="Next card"
            title="Next (→)"
            disabled={index >= surfaces.length - 1}
            onClick={() => page(1)}
          >
            <ChevronRight size={16} />
          </IconButton>
        </nav>
      )}
    </div>
  );
}
