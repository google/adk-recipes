"use client";
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

import { useCallback, useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { FailureNote } from "@/components/ui/failure-note";
import { Loading } from "@/components/ui/loading";
import { RefreshButton } from "@/components/ui/refresh-button";
import { DayFilterChip } from "@/components/ui/day-filter-chip";
import { RunInvestigationButton } from "@/components/run-investigation-button";
import { Funnel } from "@/components/stats/funnel";
import { TracesPerDay } from "@/components/stats/traces-per-day";
import { PageHeader } from "@/components/nav/page-header";
import { RunsTable } from "./runs-table";
import { HORIZON_LABEL } from "@/lib/day-buckets";
import { hasFailures } from "@/lib/run-summary";
import { newestFirst } from "@/lib/runstate";
import { isNoData } from "@/lib/failure";
import { textStyle } from "@/lib/typography";
import { cn } from "@/lib/utils";
import {
  dailyQuery,
  RUNS_LIST_LIMIT,
  runsQuery,
  statsQuery,
  type Day,
  type Run,
} from "@/lib/aqua-api";

/**
 * The investigations page: totals, the per-day chart and the newest runs.
 *
 * "Only with failures" is the caller's when it passes `onFailuresOnlyChange`,
 * and the page's own otherwise, as `InsightsView` does with its status.
 *
 * `day` swaps the newest runs for the runs one column of the per-day chart
 * counts, and `onDayClear` is how the chip naming it takes it off.
 */
export function InvestigationsView({
  failuresOnly,
  onFailuresOnlyChange,
  day,
  onDayClear,
}: {
  failuresOnly?: boolean;
  onFailuresOnlyChange?: (only: boolean) => void;
  day?: string;
  onDayClear?: () => void;
} = {}) {
  const stats = useQuery(statsQuery());
  const daily = useQuery(dailyQuery());
  const runs = useQuery(runsQuery(day));
  // What the rows are compared with, unfiltered: a day's first sweep was
  // preceded by one the day leaves out. Without a day this is `runs` itself,
  // one query under one key.
  const allRuns = useQuery(runsQuery());

  // All of these describe the same sweeps, over different spans, so refreshing
  // one and not the others would leave the page describing two moments as
  // well as two spans.
  const refresh = useCallback(() => {
    void stats.refetch();
    void daily.refetch();
    void runs.refetch();
    if (day) void allRuns.refetch();
  }, [stats, daily, runs, allRuns, day]);
  const refreshing =
    stats.isFetching ||
    daily.isFetching ||
    runs.isFetching ||
    allRuns.isFetching;

  return (
    <div className="flex flex-col gap-8">
      <PageHeader title="Investigations" />
      {/* Three reads, three independent sections: one that is slow or
            broken leaves the other two showing what they have.

            They also all fail together on a deployment that has exported no
            telemetry, which is why only this one explains the state in full --
            three copies of the same paragraph is its own kind of alarming. */}
      {stats.isError ? (
        <FailureNote
          lead="Couldn't load the totals"
          error={stats.error}
          onRetry={refresh}
        />
      ) : stats.data ? (
        <section className="flex flex-col gap-3">
          <h2 className={textStyle.sectionTitle}>
            Totals{" "}
            <span className="font-normal text-muted-foreground">
              · {HORIZON_LABEL} · {stats.data.investigations.toLocaleString()}{" "}
              {stats.data.investigations === 1
                ? "investigation"
                : "investigations"}
            </span>
          </h2>
          <Funnel stats={stats.data} />
        </section>
      ) : (
        <Loading label="Loading totals…" />
      )}
      {/* A chart with nothing to draw is already rendered as nothing, so a
            no-telemetry read is too: the state is the same one, and the
            section above has said it. Any other failure still shows. */}
      {daily.isError ? (
        isNoData(daily.error) ? null : (
          <FailureNote
            lead="Couldn't load the daily counts"
            error={daily.error}
            onRetry={refresh}
            compact
          />
        )
      ) : daily.isLoading ? (
        <Loading label="Loading daily counts…" />
      ) : (
        daily.data && daily.data.length > 0 && <PerDay days={daily.data} />
      )}
      <RunsSection
        runs={newestFirst(runs.data ?? [])}
        // The day's runs as well: an old day can fall outside the newest page.
        history={[
          ...new Map(
            [...(allRuns.data ?? []), ...(runs.data ?? [])].map((run) => [
              run.run_id,
              run,
            ]),
          ).values(),
        ]}
        loading={runs.isLoading}
        error={runs.isError ? runs.error : null}
        onRefresh={refresh}
        refreshing={refreshing}
        failuresOnly={failuresOnly}
        onFailuresOnlyChange={onFailuresOnlyChange}
        day={day}
        dayTotal={
          day
            ? daily.data?.find((d) => d.day === day)?.investigations
            : undefined
        }
        onDayClear={onDayClear}
      />
    </div>
  );
}

/** Evaluated trajectories per day, over a fixed span so quiet days stay visible. */
function PerDay({ days }: { days: Day[] }) {
  return (
    <section className="flex flex-col gap-3">
      <h2 className={textStyle.sectionTitle}>Evaluated trajectories per day</h2>
      <TracesPerDay days={days} />
      <p className={textStyle.meta}>
        Each column is that day&apos;s evaluated trajectories, split by outcome;
        the figure above it is the failures. Days are UTC.
      </p>
    </section>
  );
}

function RunsSection({
  runs,
  history,
  loading,
  error,
  onRefresh,
  refreshing,
  failuresOnly: ownedFailuresOnly,
  onFailuresOnlyChange,
  day,
  dayTotal,
  onDayClear,
}: {
  runs: Run[];
  /** Every run the rows can be compared with, whatever the filters hide. */
  history: Run[];
  loading: boolean;
  error: unknown;
  onRefresh: () => void;
  refreshing: boolean;
  failuresOnly?: boolean;
  onFailuresOnlyChange?: (only: boolean) => void;
  day?: string;
  /** How many runs the per-day chart counts on `day`, when it has said. */
  dayTotal?: number;
  onDayClear?: () => void;
}) {
  // Client-side over the page already loaded, as in the old dashboard: the
  // filter is a way of reading what is on screen, not a different request, and
  // refetching would change which sweeps are on screen while claiming to
  // narrow them.
  const [localFailuresOnly, setLocalFailuresOnly] = useState(false);
  const failuresOnly = onFailuresOnlyChange
    ? (ownedFailuresOnly ?? false)
    : localFailuresOnly;
  const setFailuresOnly = onFailuresOnlyChange ?? setLocalFailuresOnly;
  const visible = failuresOnly ? runs.filter(hasFailures) : runs;
  const checkboxRef = useRef<HTMLInputElement>(null);
  // The chip unmounts with the day, so focus goes to the control beside it.
  const clearDay = () => {
    onDayClear?.();
    checkboxRef.current?.focus();
  };

  return (
    <section className="flex flex-col gap-3">
      <div className="flex flex-wrap items-center justify-between gap-4">
        {/* Named against the totals above, which are the horizon's: this list
            is the newest page whatever it spans, or one day of it, so the two
            are allowed to cover different stretches of time and have to say
            so. With a day the chip beside the heading shows it, so the heading
            says it only to a screen reader moving by headings. */}
        <h2 className={textStyle.sectionTitle}>
          Investigations{" "}
          {day ? (
            <span className="sr-only">· {day} (UTC)</span>
          ) : (
            <span className="font-normal text-muted-foreground">
              · most recent
            </span>
          )}
        </h2>
        <div className="flex items-center gap-2">
          {day && onDayClear && <DayFilterChip day={day} onClear={clearDay} />}
          <label
            className={cn(
              textStyle.label,
              "flex cursor-pointer items-center gap-1.5",
            )}
            title="Show only investigations that failed, had eval service errors, or produced insights"
          >
            <input
              ref={checkboxRef}
              type="checkbox"
              checked={failuresOnly}
              onChange={(e) => setFailuresOnly(e.target.checked)}
              className="h-3.5 w-3.5 rounded border-border"
            />
            Only with failures
          </label>
          <RefreshButton onRefresh={onRefresh} refreshing={refreshing} />
          <RunInvestigationButton />
        </div>
      </div>
      {/* One read returns at most the server's list limit, and a day swept
          more often than that would otherwise read as complete. The chart's
          count for the day is the whole of it; a day outside the chart's span
          has no count, so a full read is the only sign of a cut. */}
      {day &&
        !loading &&
        !error &&
        (dayTotal !== undefined
          ? runs.length < dayTotal && (
              <p className={textStyle.meta}>
                Showing the newest {runs.length} of {dayTotal} investigations on{" "}
                {day} (UTC).
              </p>
            )
          : runs.length >= RUNS_LIST_LIMIT && (
              <p className={textStyle.meta}>
                Showing the newest {runs.length} investigations on {day} (UTC);
                the day may hold more.
              </p>
            ))}
      {error ? (
        <FailureNote
          lead="Couldn't load the investigations"
          error={error}
          onRetry={onRefresh}
          compact
        />
      ) : loading ? (
        <Loading label="Loading investigations…" />
      ) : runs.length === 0 ? (
        <p className={textStyle.description}>
          {day
            ? `No investigations on ${day} (UTC).`
            : "No investigations yet."}
        </p>
      ) : visible.length === 0 ? (
        // Named apart from "nothing here": every sweep passing or idling is not
        // an empty deployment, and reading it as an empty account would be the
        // wrong conclusion.
        <p className={textStyle.description}>
          {day ? `All investigations on ${day} (UTC)` : "All investigations"}{" "}
          passed every metric. Uncheck “Only with failures” to see clean and
          idle investigations.
        </p>
      ) : (
        <RunsTable runs={visible} history={history} />
      )}
    </section>
  );
}
