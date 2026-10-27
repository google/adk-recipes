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

/** When a sweep counts as still in flight, and what the Run investigation
 *  button says while one is.
 *
 * A run costs money, so the button is disabled for as long as the runs list
 * says something is already going. That makes "is anything in flight?" the one
 * judgement the control rests on, which is why it lives here as pure functions
 * over the list: nothing below touches the DOM, the clock or the network.
 */

/** What these predicates need off a run. A superset of `Run` on purpose: the
 *  legacy records have no `created_at`, and a caller may hold neither field. */
export interface RunLike {
  run_id?: string;
  status?: string | null;
  created_at?: string | null;
  /** Older records predate the execution timestamps; the telemetry window's
   *  end is the closest thing they carry to a start time. */
  window_end?: string | null;
}

/** The two statuses a run passes through before it settles. `format_run` emits
 *  them in the same vocabulary the status pill renders (done / pending /
 *  running / failed); anything else -- an unknown status, a missing one -- is
 *  treated as settled, so an unrecognised value cannot lock the button. A
 *  `scheduled` run is left out too: nothing runs for it until its trigger
 *  fires. */
export const IN_FLIGHT_STATUSES = ["pending", "running"];

/** How long a run may claim to be in flight before we stop believing it. A job
 *  that dies without writing its record leaves a `pending` row behind forever,
 *  and a button derived from that row would never come back -- not even across
 *  a reload, because the state is the server's. Half an hour is far longer than
 *  a sweep takes and short enough that a wedged record is not a lasting
 *  lockout. */
export const STALE_AFTER_MS = 30 * 60 * 1000;

/** How often the list is re-read while something is in flight. Also the
 *  duration the button's fill animates over, so the bar reaching the far edge
 *  and the refresh happening are the same event. */
export const POLL_INTERVAL_MS = 30000;

/** When a run started, by whichever timestamp the record carries, or null
 *  when it carries neither. */
export function startedAt(run: RunLike | null | undefined): number | null {
  const t = Date.parse((run && (run.created_at || run.window_end)) || "");
  return Number.isFinite(t) ? t : null;
}

function isUnsettled(run: RunLike | null | undefined): boolean {
  return IN_FLIGHT_STATUSES.includes(String(run?.status || "").toLowerCase());
}

/** A run we are still waiting on: unsettled status, and not so old that the
 *  record has to be assumed abandoned. A run with no usable timestamp counts as
 *  in flight -- it cannot be shown to be stale, and treating "unknown" as
 *  expired would re-enable the button in exactly the case where a second sweep
 *  is most likely to duplicate the first. */
export function isInFlight(
  run: RunLike | null | undefined,
  now: number,
  staleAfterMs: number = STALE_AFTER_MS,
): boolean {
  if (!isUnsettled(run)) return false;
  const started = startedAt(run);
  if (started === null) return true;
  return !(now - started > staleAfterMs);
}

export function inFlightRuns<T extends RunLike>(
  runs: readonly T[] | null | undefined,
  now: number = Date.now(),
  staleAfterMs: number = STALE_AFTER_MS,
): T[] {
  return (runs || []).filter((run) => isInFlight(run, now, staleAfterMs));
}

/** A run the record still calls pending or running, and that we have stopped
 *  waiting on. Worth naming rather than just ignoring: the row says "running"
 *  while the button is available, and a reader is owed the reason. */
export function isStalled(
  run: RunLike | null | undefined,
  now: number = Date.now(),
  staleAfterMs: number = STALE_AFTER_MS,
): boolean {
  return isUnsettled(run) && !isInFlight(run, now, staleAfterMs);
}

export function stalledRuns<T extends RunLike>(
  runs: readonly T[] | null | undefined,
  now: number = Date.now(),
  staleAfterMs: number = STALE_AFTER_MS,
): T[] {
  return (runs || []).filter((run) => isStalled(run, now, staleAfterMs));
}

/** How long a run has been claiming to be in flight, coarsely -- "45m", "6h",
 *  "2d". Coarse on purpose: the figure is context for "this is not moving", and
 *  a live-looking "6h 04m" beside a run nothing is watching would be a
 *  fiction. */
export function inFlightAge(
  run: RunLike | null | undefined,
  now: number = Date.now(),
): string {
  const started = startedAt(run);
  if (started === null) return "";
  const minutes = Math.floor((now - started) / 60000);
  if (minutes < 1) return "under a minute";
  if (minutes < 60) return `${minutes}m`;
  const hours = Math.floor(minutes / 60);
  return hours < 48 ? `${hours}h` : `${Math.floor(hours / 24)}d`;
}

/** What the button reads while it waits. The count is named rather than
 *  implied: an ambient sweep can start one without anybody clicking, so
 *  "2 running" tells a reader who pressed the button once that the other one is
 *  not theirs. */
export function runButtonLabel(inFlightCount: number): string {
  const n = Number(inFlightCount) || 0;
  if (n <= 0) return "Run investigation";
  return n === 1 ? "Running…" : `${n} running…`;
}

/** A copy of the runs, newest first by whichever start timestamp each record
 *  carries, with the undated ones last, in the order given. By time, not by
 *  string: offsets written as "Z" and as "+00:00" do not sort as text. */
export function newestFirst<T extends RunLike>(runs: readonly T[]): T[] {
  return [...runs].sort((a, b) => {
    const ta = startedAt(a) ?? -Infinity;
    const tb = startedAt(b) ?? -Infinity;
    // Equal first: two undated runs would otherwise give -Infinity - -Infinity,
    // which is NaN.
    return ta === tb ? 0 : tb - ta;
  });
}

/** The newest of a set, by whichever start timestamp the record carries. */
export function newestRun<T extends RunLike>(runs: readonly T[]): T | null {
  return runs.reduce<T | null>(
    (best, run) =>
      (startedAt(run) ?? 0) > (startedAt(best) ?? 0) ? run : best,
    runs[0] ?? null,
  );
}
