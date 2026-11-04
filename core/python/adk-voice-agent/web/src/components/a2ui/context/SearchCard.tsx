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

import { Caption, type ContextCardProps, Footer } from "./parts";

function domainOf(source: { domain?: string; url: string }): string {
  if (source.domain) return source.domain.replace(/^www\./, "");
  try {
    return new URL(source.url).hostname.replace(/^www\./, "");
  } catch {
    return source.url;
  }
}

/** A grounded answer to read, the sites it came from, and a way to keep searching. */
export function SearchCard({
  surface,
  items,
  bullets,
  meta,
}: ContextCardProps) {
  return (
    <>
      <Caption>“{meta.query ?? surface.subtitle}”</Caption>
      <p className="search-answer">{bullets[0]}</p>
      {items.length > 0 && (
        <ul className="search-sources" aria-label="Sources">
          {items.map((source) => {
            const domain = domainOf(source as { domain?: string; url: string });
            return (
              <li key={source.url}>
                <a
                  className="source-chip"
                  href={source.url}
                  target="_blank"
                  rel="noreferrer"
                  title={source.title}
                >
                  <span className="source-letter" aria-hidden>
                    {domain[0]?.toUpperCase()}
                  </span>
                  {domain}
                </a>
              </li>
            );
          })}
        </ul>
      )}
      <Footer href={meta.search_url} label="Search on Google" />
    </>
  );
}
