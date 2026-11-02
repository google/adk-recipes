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

import { MapPin } from "lucide-react";
import { Caption, type ContextCardProps, Footer, Row, Rows } from "./parts";

/** Places to pick from: name, a line about each, and a link. Maps grounding returns no ratings or hours. */
export function MapsCard({ surface, items, bullets, meta }: ContextCardProps) {
  return (
    <>
      <Caption>“{meta.query ?? surface.subtitle}”</Caption>
      {items.length === 0 ? (
        <p className="search-answer">{bullets[0]}</p>
      ) : (
        <Rows>
          {items.map((place) => (
            <Row key={place.url} href={place.url}>
              <span className="place-pin" aria-hidden>
                <MapPin size={15} />
              </span>
              <div className="place-main">
                <p className="place-name">{place.name}</p>
                {place.summary && (
                  <p className="place-summary">{place.summary}</p>
                )}
              </div>
            </Row>
          ))}
        </Rows>
      )}
      <Footer href={meta.maps_url} label="Open in Maps" />
    </>
  );
}
