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

import { ArrowUpRight } from "lucide-react";
import type { ReactNode } from "react";
import { safeHref } from "../../../lib/answerCards";
import type { A2UISurface, LooseRecord } from "../../../lib/api";

export { safeHref };

/** One row of a context card's data model: a search source or a place. */
export type Item = LooseRecord;

/** The parts of a context card's data model that the per-app cards render. */
export interface ContextCardProps {
  surface: A2UISurface;
  items: Item[];
  meta: LooseRecord;
  bullets: string[];
}

/** Rows separated by hairlines, edge to edge inside the card. */
export function Rows({ children }: { children: ReactNode }) {
  return (
    <section className="ctx-group">
      <ul className="ctx-rows">{children}</ul>
    </section>
  );
}

/** A row that opens its item in the app; a plain row when there's no link. */
export function Row({
  href,
  children,
}: {
  href?: string | null;
  children: ReactNode;
}) {
  const safe = safeHref(href);
  return (
    <li>
      {safe ? (
        <a className="ctx-row" href={safe} target="_blank" rel="noreferrer">
          {children}
        </a>
      ) : (
        <div className="ctx-row">{children}</div>
      )}
    </li>
  );
}

/** A quiet sentence above the rows: what was asked, or what happened. */
export function Caption({ children }: { children: ReactNode }) {
  if (!children) return null;
  return <p className="ctx-caption">{children}</p>;
}

/** The card's last line: a link out on the right. */
export function Footer({ href, label }: { href?: string; label?: string }) {
  const safe = safeHref(href);
  if (!safe) return null;
  return (
    <footer className="ctx-foot">
      <a className="ctx-link" href={safe} target="_blank" rel="noreferrer">
        {label}
        <ArrowUpRight size={13} aria-hidden />
      </a>
    </footer>
  );
}
