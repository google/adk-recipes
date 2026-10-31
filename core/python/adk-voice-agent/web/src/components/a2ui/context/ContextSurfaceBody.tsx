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

import type { A2UISurface } from "../../../lib/api";
import { type AnswerCardData, isCardKind } from "../../../lib/cards";
import { keyedByText } from "../../../lib/keys";
import { AnswerCard } from "../answer/AnswerCard";
import { MapsCard } from "./MapsCard";
import { type ContextCardProps, Row, Rows } from "./parts";
import { SearchCard } from "./SearchCard";

/** Any other tool result: its summary lines. */
function BulletCard({ bullets }: ContextCardProps) {
  return (
    <Rows>
      {keyedByText(bullets).map(({ key, item: text }) => (
        <Row key={key}>
          <p className="ctx-bullet">{text}</p>
        </Row>
      ))}
    </Rows>
  );
}

/** One design per grounding tool that records cards (`app/tools/grounding_tools.py`), chosen by `subkind`. */
const CARDS: Array<
  [match: string, Card: (p: ContextCardProps) => JSX.Element]
> = [
  ["search", SearchCard],
  ["maps", MapsCard],
];

export function ContextSurfaceBody({ surface }: { surface: A2UISurface }) {
  const ctx = surface.dataModel.context;
  const props: ContextCardProps = {
    surface,
    items: Array.isArray(ctx?.items) ? ctx.items : [],
    meta: ctx?.meta && typeof ctx.meta === "object" ? ctx.meta : {},
    bullets: Array.isArray(ctx?.bullets) ? ctx.bullets : [],
  };
  const subkind = (surface.subkind || ctx?.brandIcon || "").toLowerCase();
  if (isCardKind(subkind)) {
    // An answer card from `look`: the header already names the app, so the
    // subtitle (what was asked) heads the list.
    return (
      <div className="ctx-body">
        <AnswerCard
          kind={subkind}
          title={surface.subtitle || undefined}
          items={props.items as AnswerCardData["items"]}
        />
      </div>
    );
  }
  const Card =
    CARDS.find(([match]) => subkind.includes(match))?.[1] ?? BulletCard;
  return (
    <div className="ctx-body">
      <Card {...props} />
    </div>
  );
}
