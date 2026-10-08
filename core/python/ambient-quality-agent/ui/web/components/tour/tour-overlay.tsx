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

import { useEffect, useId, useRef, useState } from "react";
import { type QueryClient, useQueryClient } from "@tanstack/react-query";
import { useRouter } from "@tanstack/react-router";
import { MousePointer2, X } from "lucide-react";
import { Button } from "@/components/ui/button";
import {
  Popover,
  PopoverAnchor,
  PopoverContent,
} from "@/components/ui/popover";
import { textStyle } from "@/lib/typography";
import { cn } from "@/lib/utils";
import {
  endTour,
  showTourStep,
  startTour,
  useTourStep,
} from "@/lib/tour-state";
import { HOME_SIDE_BY_SIDE, tourSteps, type TourStep } from "./tour-steps";

/** How far the spotlight's ring stands off the element it frames, in px. */
const RING_GAP = 6;

/** How far the callout stands off the element, clear of the ring, in px. */
const CALLOUT_OFFSET = RING_GAP + 8;

/** The callout's width, in px: narrow enough for a phone. */
const CALLOUT_WIDTH = 320;

/** What covers the rest of the page while a step is open. */
const SCRIM = "rgb(0 0 0 / 0.55)";

/** How long the spotlight takes to glide from one element to the next, in ms. */
const GLIDE_MS = 500;

/** The pointer's way to the link that leads to the next page, in ms: its
 *  travel there, a pause on the link so its label can be read, the press, and
 *  its fade once the page has changed. Slow enough to follow with the eye,
 *  quick enough not to be waited for. */
const TRAVEL_MS = 650;
const PAUSE_MS = 250;
const PRESS_MS = 350;
const FADE_MS = 250;

/** How long the spotlight holds its place while the next step's element is
 *  missing, in ms: long enough for a new page to render it, so the spotlight
 *  glides on from the link it clicked; short enough not to strand it when the
 *  element never comes, as the evidence does not for an insight that has no
 *  occurrence yet. */
const GRACE_MS = 900;

/**
 * The guided tour, drawn over the dashboard: a spotlight on the step's element
 * and a callout beside it that walks from one feature to the next. When the
 * next feature is on another page, a pointer travels to the link that leads
 * there and clicks it, so the reader sees the way rather than being carried.
 * Mounted once, in the root route; the tab bar's button opens it, and so does
 * `?tour` in the address, so a link can hand someone the tour rather than the
 * dashboard.
 *
 * The page underneath stays usable: the scrim does not take clicks, so a reader
 * can try what a step points at and carry on with Next.
 */
export function TourOverlay() {
  const index = useTourStep();
  const router = useRouter();

  useEffect(() => {
    if ("tour" in router.state.location.search) startTour();
  }, [router]);

  if (index === null) return null;
  return <OpenTour index={index} />;
}

/** The link the tour clicks on the way to the next step's page, and how far
 *  along the click is. */
interface Via {
  link: HTMLElement;
  /** What the link says, for the label beside the pointer. */
  label: string;
  /** Where the pointer sets off from: the button that was pressed. */
  from: DOMRect | null;
  phase: "travel" | "press" | "fade";
}

function OpenTour({ index }: { index: number }) {
  // Settled when the tour opens, so a step keeps its number if the window is
  // resized across the breakpoint while it is open.
  const [steps] = useState(() =>
    tourSteps(window.matchMedia?.(HOME_SIDE_BY_SIDE).matches ?? true),
  );
  const step = steps[index];
  const router = useRouter();
  const client = useQueryClient();
  const titleId = useId();
  const bodyId = useId();
  const primary = useRef<HTMLButtonElement>(null);
  // Which way a skipped step passes the reader on: forward after Next, back
  // after Back, so Back never lands on the step it just left.
  const direction = useRef<1 | -1>(1);
  const pressed = useRef<DOMRect | null>(null);
  // The step whose page is open, set once the tour has gone there.
  const [arrivedAt, setArrivedAt] = useState<number | null>(null);
  const [via, setVia] = useState<Via | null>(null);
  const onPage = arrivedAt === index;

  const go = (to: number, from?: Element | null) => {
    pressed.current =
      (from ?? primary.current)?.getBoundingClientRect() ?? null;
    direction.current = to < index ? -1 : 1;
    if (to >= steps.length) endTour();
    else showTourStep(Math.max(0, to));
  };

  // biome-ignore lint/correctness/useExhaustiveDependencies: runs once per step; step and go follow from index, the pathname is read when the step opens, and the router and query client live as long as the app.
  useEffect(() => {
    let current = true;
    const timers: number[] = [];
    const wait = (ms: number) =>
      new Promise((resolve) => timers.push(window.setTimeout(resolve, ms)));
    const arrive = () => setArrivedAt(index);
    void (async () => {
      const page = await resolvePage(step, client);
      if (!current) return;
      if (page === null) {
        go(index + direction.current);
        return;
      }
      if (page === undefined || page === router.state.location.pathname) {
        arrive();
        return;
      }
      const link = prefersReducedMotion() ? null : findLinkTo(page);
      if (!link) {
        void router.navigate({ href: page });
        arrive();
        return;
      }
      const way = { link, label: describeLink(link), from: pressed.current };
      setVia({ ...way, phase: "travel" });
      await wait(TRAVEL_MS + PAUSE_MS);
      if (!current) return;
      setVia({ ...way, phase: "press" });
      await wait(PRESS_MS);
      if (!current) return;
      // The link's own click, so the tour goes exactly where a reader
      // pressing it would.
      link.click();
      arrive();
      setVia({ ...way, phase: "fade" });
      await wait(FADE_MS);
      if (current) setVia(null);
    })();
    return () => {
      current = false;
      for (const timer of timers) window.clearTimeout(timer);
      setVia(null);
    };
  }, [index]);

  // Escape ends the tour from anywhere, including while the pointer is on
  // its way and the callout is not there to hear it.
  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") endTour();
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, []);

  const spotlight = useSpotlight(
    via && via.phase !== "fade" ? via.link : onPage ? step.target : null,
  );
  const placement = placeCallout(
    spotlight.box,
    window.innerWidth,
    window.innerHeight,
  );
  // The popover reads the anchor through a ref, so the anchor keeps one
  // identity and the popover repositions itself instead of remounting.
  const placementRef = useRef(placement);
  placementRef.current = placement;
  const anchor = useRef({
    getBoundingClientRect: () => placementRef.current.anchor,
  });
  // The callout fades out while the spotlight glides, rather than closing, so
  // focus stays on Next, and fades back in beside the next element once the
  // spotlight rests there. While the pointer is on its way to another page it
  // is closed.
  const hidden = via !== null || !onPage || !spotlight.arrived;
  // The step whose words the callout shows: the one it is leaving until it has
  // faded out, so the words never change in sight.
  const [shownIndex, setShownIndex] = useState(index);
  if (!hidden && shownIndex !== index) setShownIndex(index);
  const shown = steps[shownIndex];
  const { hole } = spotlight;

  return (
    <>
      {/* Square, because a spread shadow grows the corner radius by its own
          size: a rounded box with this shadow leaves the far corners of the
          window undimmed. The rounded ring is drawn inside it instead.
          transition-none because the glide moves it frame by frame: the
          fade-in's duration-500 would otherwise transition every property,
          and the hole would trail behind the element. */}
      <div
        aria-hidden="true"
        data-testid={hole.width > 0 ? "tour-spotlight" : "tour-scrim"}
        className="pointer-events-none fixed z-40 transition-none motion-safe:animate-in motion-safe:fade-in-0 motion-safe:duration-500"
        style={{
          left: hole.left,
          top: hole.top,
          width: hole.width,
          height: hole.height,
          boxShadow: `0 0 0 100vmax ${SCRIM}`,
        }}
      >
        {hole.width > 0 && (
          <div className="absolute inset-0 rounded-lg shadow-[0_0_28px_hsl(var(--primary)/0.35)] ring-2 ring-primary" />
        )}
      </div>
      {via && <TourPointer via={via} />}
      <Popover open={via === null}>
        <PopoverAnchor virtualRef={anchor} />
        <PopoverContent
          data-tour-callout=""
          data-hidden={hidden ? "" : undefined}
          side={placement.side}
          align={placement.align}
          sideOffset={CALLOUT_OFFSET}
          collisionPadding={12}
          // The anchor is not an element the popover can watch, so it follows
          // the box every frame as the page scrolls.
          updatePositionStrategy="always"
          aria-labelledby={titleId}
          aria-describedby={bodyId}
          onOpenAutoFocus={(event) => {
            event.preventDefault();
            primary.current?.focus();
          }}
          // Clicking the page is trying it out, not leaving the tour.
          onInteractOutside={(event) => event.preventDefault()}
          onKeyDown={(event) => {
            if (event.key === "ArrowRight") go(index + 1);
            else if (event.key === "ArrowLeft" && index > 0) go(index - 1);
          }}
          style={{ width: CALLOUT_WIDTH }}
          className="p-4 motion-safe:transition-opacity motion-safe:duration-200 data-[hidden]:pointer-events-none data-[hidden]:opacity-0 motion-safe:data-[hidden]:duration-100"
        >
          <div className="flex items-start justify-between gap-3">
            <h2 id={titleId} className={textStyle.sectionTitle}>
              {shown.title}
            </h2>
            <button
              type="button"
              onClick={endTour}
              aria-label="End the tour"
              className="-m-1 grid h-6 w-6 shrink-0 place-items-center rounded-md text-muted-foreground hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
            >
              <X className="h-3.5 w-3.5" aria-hidden="true" />
            </button>
          </div>
          <p id={bodyId} className={cn(textStyle.meta, "mt-1.5")}>
            {shown.body}
          </p>
          {/* The dialog is announced when it opens; a step that follows on
              the same page changes only its words, so they are read out here. */}
          <p className="sr-only" aria-live="polite">
            {`${step.title}. ${step.body}`}
          </p>
          <div className="mt-4 flex items-center justify-between gap-3">
            <span className={textStyle.meta}>
              {shownIndex + 1} of {steps.length}
            </span>
            <div className="flex items-center gap-2">
              {shownIndex > 0 && (
                <Button
                  size="sm"
                  variant="ghost"
                  onClick={(event) => go(index - 1, event.currentTarget)}
                >
                  Back
                </Button>
              )}
              <Button
                ref={primary}
                size="sm"
                onClick={(event) => go(index + 1, event.currentTarget)}
              >
                {shownIndex === steps.length - 1 ? "Done" : "Next"}
              </Button>
            </div>
          </div>
        </PopoverContent>
      </Popover>
    </>
  );
}

/**
 * The pointer that shows where the tour clicks to change pages: it sets off
 * from the button the reader pressed, travels to the link on the spotlight's
 * curve, names it, presses it with a ripple, and fades as the page changes.
 * Never shown under reduced motion, where the tour changes pages without it.
 */
function TourPointer({ via }: { via: Via }) {
  const { link, from, phase } = via;
  const [at, setAt] = useState(() =>
    centerOf(from ?? link.getBoundingClientRect()),
  );
  // Near the right edge the label goes to the pointer's left, or it would be
  // cut off by the window.
  const [labelLeft] = useState(
    () => link.getBoundingClientRect().right > window.innerWidth - 220,
  );

  // Moved frame by frame rather than by a transition, so it keeps to the link
  // when the page scrolls the link into view on the way.
  useEffect(() => {
    const start = centerOf(from ?? link.getBoundingClientRect());
    const setOff = performance.now();
    let frame = 0;
    const follow = () => {
      // Once the click has taken the link off the page, the pointer stays
      // where it clicked.
      if (link.isConnected) {
        const end = centerOf(link.getBoundingClientRect());
        const t = ease(Math.min(1, (performance.now() - setOff) / TRAVEL_MS));
        const next = {
          x: start.x + (end.x - start.x) * t,
          y: start.y + (end.y - start.y) * t,
        };
        setAt((drawn) =>
          drawn.x === next.x && drawn.y === next.y ? drawn : next,
        );
      }
      frame = requestAnimationFrame(follow);
    };
    frame = requestAnimationFrame(follow);
    return () => cancelAnimationFrame(frame);
  }, [link, from]);

  return (
    <div
      data-testid="tour-pointer"
      aria-hidden="true"
      className="pointer-events-none fixed left-0 top-0 z-[60]"
      // The arrow's tip sits 4px in from the icon's corner.
      style={{ transform: `translate(${at.x - 4}px, ${at.y - 4}px)` }}
    >
      {/* The fades are on this inner layer: the fade-in's keyframes also set a
          transform, which would carry the pointer in from the window's corner. */}
      <div
        className={cn(
          "animate-in fade-in-0 transition-opacity duration-200",
          phase === "fade" && "opacity-0",
        )}
      >
        {phase !== "travel" && (
          <span className="absolute -left-4 -top-4 h-8 w-8 rounded-full bg-primary/40 animate-[ping_600ms_cubic-bezier(0,0,0.2,1)_1_forwards]" />
        )}
        <MousePointer2
          className={cn(
            "h-[22px] w-[22px] fill-white text-neutral-900 drop-shadow-md transition-transform duration-150",
            phase === "press" && "scale-[0.82]",
          )}
        />
        <span
          className={cn(
            textStyle.label,
            "absolute top-6 whitespace-nowrap rounded-md border bg-popover px-2 py-1 text-popover-foreground shadow-md",
            labelLeft ? "right-6" : "left-5",
          )}
        >
          Click “{via.label}”
        </span>
      </div>
    </div>
  );
}

/** The page a step is on: its path, undefined to stay on the open page, or null
 *  when the deployment has nothing for the step to show. A read that fails
 *  counts as nothing to show, so the tour carries on past it. */
async function resolvePage(
  step: TourStep,
  client: QueryClient,
): Promise<string | null | undefined> {
  if (typeof step.page !== "function") return step.page;
  try {
    return await step.page(client);
  } catch {
    return null;
  }
}

/** The link a reader would press to reach `page`: one on screen, looked for
 *  first in the page's content and then anywhere, so a link the page offers
 *  wins over the same one in the sidebar. Null when the open page has no such
 *  link. */
function findLinkTo(page: string): HTMLElement | null {
  const scopes = [document.querySelector("main"), document];
  for (const scope of scopes) {
    for (const link of scope?.querySelectorAll<HTMLElement>(
      `a[href="${page}"]`,
    ) ?? []) {
      const rect = link.getBoundingClientRect();
      if (rect.width > 0 && rect.height > 0) return link;
    }
  }
  return null;
}

/** What a link says, as the reader sees it: its label when it is an icon, its
 *  first line of text otherwise, shortened to fit beside the pointer. */
function describeLink(link: HTMLElement): string {
  const text = (
    link.getAttribute("aria-label") ||
    link.innerText ||
    link.textContent ||
    ""
  )
    .trim()
    .split("\n")[0];
  return text.length > 32 ? `${text.slice(0, 31).trimEnd()}…` : text;
}

/** The spotlight as it is drawn on one frame. */
interface Spotlight {
  /** The framed element's box, or null while there is none on the page. */
  box: DOMRect | null;
  /** The hole in the scrim: the element's box with the ring's gap around it,
   *  or somewhere on the way there; a point where the callout opens when there
   *  is no element. */
  hole: DOMRect;
  /** True once the hole has come to rest on the element, or on that point. */
  arrived: boolean;
}

/**
 * The spotlight on the element marked `data-tour={locate}`, or on the element
 * itself, or held where it is while `locate` is null. The element is followed
 * every frame as the page scrolls and reflows, and scrolled into view the
 * first time it is found unless it is in view already. The hole glides to it
 * over GLIDE_MS from wherever the last element left it, then keeps to it
 * exactly. While the element is missing the hole holds its place for GRACE_MS,
 * so it can glide on from there once a new page renders the element; after
 * that the box is null and the hole closes to the point where the callout
 * opens.
 */
function useSpotlight(locate: string | HTMLElement | null): Spotlight {
  const [state, setState] = useState<Spotlight & { locate: typeof locate }>(
    () => ({
      locate,
      box: null,
      hole: restingPoint(),
      arrived: false,
    }),
  );
  // Where the hole was last drawn, which the next glide sets off from.
  const drawn = useRef(state.hole);

  useEffect(() => {
    if (locate === null) return;
    const glideMs = prefersReducedMotion() ? 0 : GLIDE_MS;
    let frame = 0;
    let scrolled = false;
    let missingSince: number | null = null;
    let box: DOMRect | null = null;
    let hole = drawn.current;
    let arrived = false;
    // A glide sets off afresh from wherever the hole is each time it changes
    // course: to the element once it is found, to the resting point once the
    // element is given up on.
    let course: "element" | "rest" | null = null;
    let from = hole;
    let setOff = 0;
    const follow = () => {
      const now = performance.now();
      const element =
        typeof locate === "string"
          ? document.querySelector(`[data-tour="${locate}"]`)
          : locate;
      const rect = element?.isConnected
        ? element.getBoundingClientRect()
        : null;
      let goal: DOMRect | null = null;
      if (element && rect && rect.width > 0 && rect.height > 0) {
        missingSince = null;
        if (!scrolled) {
          scrolled = true;
          const inView =
            rect.top >= 0 &&
            rect.left >= 0 &&
            rect.bottom <= window.innerHeight &&
            rect.right <= window.innerWidth;
          if (!inView)
            element.scrollIntoView({
              block: "center",
              behavior: glideMs ? "smooth" : "auto",
            });
        }
        box = rect;
        goal = new DOMRect(
          rect.x - RING_GAP,
          rect.y - RING_GAP,
          rect.width + 2 * RING_GAP,
          rect.height + 2 * RING_GAP,
        );
      } else {
        missingSince ??= now;
        if (now - missingSince > GRACE_MS) {
          box = null;
          goal = restingPoint();
        }
      }
      // Without a goal the element is only briefly missing, and everything
      // holds as it is.
      if (goal) {
        const heading = box ? "element" : "rest";
        if (heading !== course) {
          course = heading;
          from = hole;
          setOff = now;
        }
        const progress = glideMs ? Math.min(1, (now - setOff) / glideMs) : 1;
        hole = progress === 1 ? goal : mix(from, goal, ease(progress));
        arrived = progress === 1;
      }
      drawn.current = hole;
      setState((shown) =>
        shown.locate === locate &&
        shown.arrived === arrived &&
        isSameBox(shown.hole, hole) &&
        isSameBox(shown.box, box)
          ? shown
          : { locate, box, hole, arrived },
      );
      frame = requestAnimationFrame(follow);
    };
    follow();
    return () => cancelAnimationFrame(frame);
  }, [locate]);

  // What was drawn for an earlier element has not arrived at this one.
  return {
    box: state.box,
    hole: state.hole,
    arrived: state.arrived && state.locate === locate,
  };
}

/** Where the callout opens when there is no element to point at, which is
 *  where the spotlight's hole closes to. */
function restingPoint(): DOMRect {
  return placeCallout(null, window.innerWidth, window.innerHeight).anchor;
}

function isSameBox(a: DOMRect | null, b: DOMRect | null): boolean {
  if (a === null || b === null) return a === b;
  return (
    a.x === b.x && a.y === b.y && a.width === b.width && a.height === b.height
  );
}

/** The rectangle `t` of the way from `a` to `b`, for `t` from 0 to 1. */
function mix(a: DOMRect, b: DOMRect, t: number): DOMRect {
  const at = (start: number, end: number) => start + (end - start) * t;
  return new DOMRect(
    at(a.x, b.x),
    at(a.y, b.y),
    at(a.width, b.width),
    at(a.height, b.height),
  );
}

/** The curve the spotlight and the pointer move on, for `t` from 0 to 1: they
 *  set off gently, and come to rest gently. */
function ease(t: number): number {
  return t < 0.5 ? 4 * t ** 3 : 1 - (-2 * t + 2) ** 3 / 2;
}

function centerOf(rect: DOMRect): { x: number; y: number } {
  return { x: rect.left + rect.width / 2, y: rect.top + rect.height / 2 };
}

function prefersReducedMotion(): boolean {
  return (
    window.matchMedia?.("(prefers-reduced-motion: reduce)").matches ?? false
  );
}

/** Where the callout opens: the rectangle it is anchored to, and on which side
 *  and alignment. */
export interface CalloutPlacement {
  anchor: DOMRect;
  side: "top" | "bottom" | "left" | "right";
  align: "start" | "center";
}

/** Places the callout in a window `width` by `height`: below the step's
 *  element; beside it, on a side with room, when the element is taller than
 *  half the window; and at the bottom of the window, over the element, when a
 *  tall element leaves no room beside it either, as a section does on a
 *  phone. With no element to point at, the callout opens centered, a
 *  third of the way down, so it reads as a message rather than a label. */
export function placeCallout(
  box: DOMRect | null,
  width: number,
  height: number,
): CalloutPlacement {
  if (!box)
    return {
      anchor: new DOMRect(width / 2, height / 3, 0, 0),
      side: "bottom",
      align: "center",
    };
  if (box.height <= height / 2)
    return { anchor: box, side: "bottom", align: "start" };
  const room = CALLOUT_WIDTH + 2 * CALLOUT_OFFSET;
  if (width - box.right >= room)
    return { anchor: box, side: "right", align: "start" };
  if (box.left >= room) return { anchor: box, side: "left", align: "start" };
  return {
    anchor: new DOMRect(width / 2, height, 0, 0),
    side: "top",
    align: "center",
  };
}
