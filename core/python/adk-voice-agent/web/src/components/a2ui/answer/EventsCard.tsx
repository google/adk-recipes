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

import { Fragment, type ReactNode } from "react";
import {
  eventDayLabel,
  eventTimes,
  minutesLabel,
  phaseLabel,
  safeHref,
  type TimedEvent,
  timedEvents,
} from "../../../lib/answerCards";
import type { EventItem } from "../../../lib/cards";
import { MINUTE, plural } from "../../../lib/format";
import { keyedByText } from "../../../lib/keys";

/** The event's row, opening it in Calendar when it has a link. */
function EventLink({ href, children }: { href?: string; children: ReactNode }) {
  const safe = safeHref(href);
  return safe ? (
    <a className="cal-row" href={safe} target="_blank" rel="noreferrer">
      {children}
    </a>
  ) : (
    <div className="cal-row">{children}</div>
  );
}

function EventRow({ event, now }: { event: TimedEvent; now: number }) {
  const { item, start, end, phase } = event;
  const [from, to] = eventTimes(event);
  const label = phaseLabel(event, now);
  const people = item.attendees?.length ?? 0;
  const duration = !item.all_day && end > start ? (end - start) / MINUTE : 0;
  const facts = [
    !label && duration > 0 && minutesLabel(duration),
    people > 1 && plural(people, "person", "people"),
    item.location,
  ].filter(Boolean);
  return (
    <li className="cal-event" data-phase={phase}>
      <EventLink href={item.html_link}>
        <time className="cal-time" dateTime={item.start}>
          {from}
          {to && <span className="cal-time-end">{to}</span>}
        </time>
        <div className="cal-body">
          <p className="cal-title">{item.summary || "(No title)"}</p>
          {(label || facts.length > 0) && (
            <p className="cal-facts">
              {label && <span className="cal-phase">{label}</span>}
              {facts.length > 0 && <span>{facts.join(" · ")}</span>}
            </p>
          )}
        </div>
      </EventLink>
    </li>
  );
}

/**
 * Events as a timeline: a time column, the current or next meeting brought
 * forward with "Now · 28 min left" or "In 43 min", a now-line on a single day,
 * and day headings when the events span days.
 */
export function EventsCard({ items }: { items: EventItem[] }) {
  const now = Date.now();
  const events = timedEvents(items, now);
  const days = events.map((e) => eventDayLabel(e.start, now));
  const multiDay = new Set(days).size > 1;
  const nowAt = multiDay
    ? -1
    : events.findIndex((e) => e.phase !== "allday" && e.start > now);
  return (
    <ol className="cal-timeline">
      {keyedByText(events, (e) => e.item.id).map(({ key, item: event }, i) => (
        <Fragment key={key}>
          {multiDay && days[i] !== days[i - 1] && (
            <li className="cal-day">{days[i]}</li>
          )}
          {i === nowAt && (
            <li className="cal-now" aria-label="Now">
              <span>Now</span>
            </li>
          )}
          <EventRow event={event} now={now} />
        </Fragment>
      ))}
    </ol>
  );
}
