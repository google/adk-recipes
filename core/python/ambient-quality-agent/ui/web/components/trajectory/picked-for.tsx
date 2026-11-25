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

import {
  CircleX,
  Coins,
  Hourglass,
  type LucideIcon,
  Repeat,
  Scissors,
  Wrench,
} from "lucide-react";
import type { SamplingSignals } from "@/lib/aqua-api";
import {
  formatPickedSignal,
  listPickedFor,
  type PickedSignal,
} from "@/lib/estimates";
import { textStyle } from "@/lib/typography";
import { cn } from "@/lib/utils";

/** The icon of each risk signal, shared by the trajectory page and the
 *  insight page's trajectory chips. */
const SIGNAL_ICONS: Record<PickedSignal["key"], LucideIcon> = {
  failed_call: CircleX,
  cut_off_response: Scissors,
  tool_reported_failure: Wrench,
  repeated_tool_calls: Repeat,
  high_token_usage: Coins,
  slow_turn: Hourglass,
};

/** The icon of one risk signal, in the amber the dashboard uses for warnings.
 *
 * @param props.signal - The signal.
 * @param props.className - Extra classes, such as a size.
 * @returns The icon, hidden from screen readers, which read the label.
 */
export function PickedSignalIcon({
  signal,
  className,
}: {
  signal: PickedSignal;
  className?: string;
}) {
  const Icon = SIGNAL_ICONS[signal.key];
  return (
    <Icon
      aria-hidden
      className={cn("h-3.5 w-3.5 shrink-0 text-amber-500", className)}
    />
  );
}

/** Says which risk signals made weighted sampling likelier to pick a
 *  trajectory: "Prioritized for" and a chip per signal, such as "High token
 *  usage" with its rank "top 5%". Each chip explains its signal on hover.
 *
 * Renders nothing for a trajectory picked at the base weight, or under uniform
 * sampling, where no signal raised its chance.
 *
 * @param props.signals - The trajectory's `sampling_signals`.
 * @param props.probability - Its `inclusion_probability`, shown on hover.
 * @returns The line, or null.
 */
export function PickedFor({
  signals,
  probability,
}: {
  signals: SamplingSignals | null | undefined;
  probability?: number | null;
}) {
  const picked = listPickedFor(signals);
  if (picked.length === 0) return null;
  const percent = probability == null ? null : probability * 100;
  const chance =
    percent == null
      ? ""
      : ` Its chance of being picked was ${percent < 1 ? "under 1" : Math.round(percent)}%.`;
  return (
    <div
      data-testid="picked-for"
      className="mt-1 flex flex-wrap items-center gap-1.5"
    >
      <span
        className={textStyle.meta}
        title={`Risk signals made weighted sampling likelier to pick this trajectory.${chance}`}
      >
        Prioritized for
      </span>
      <ul className="contents">
        {picked.map((signal) => (
          <li
            key={signal.key}
            title={
              signal.rank
                ? `${signal.description} This conversation: ${signal.rank}.`
                : signal.description
            }
            aria-label={formatPickedSignal(signal)}
            className={cn(
              textStyle.meta,
              "inline-flex items-center gap-1.5 rounded-full border bg-muted/60 py-0.5 pr-2.5 pl-2 text-foreground",
            )}
          >
            <PickedSignalIcon signal={signal} />
            <span>{signal.label}</span>
            {signal.rank && (
              <span className="border-l pl-1.5 text-muted-foreground">
                {signal.rank}
              </span>
            )}
          </li>
        ))}
      </ul>
    </div>
  );
}
