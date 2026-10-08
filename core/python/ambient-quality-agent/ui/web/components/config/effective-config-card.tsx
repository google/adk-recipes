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
import { useQuery } from "@tanstack/react-query";
import { AlertCircle } from "lucide-react";

import { effectiveConfigQuery } from "@/lib/aqua-api";
import { configLabel, configValue, groupConfig } from "@/lib/config-fields";
import { Loading } from "@/components/ui/loading";
import { RawJson } from "@/components/ui/raw-json";
import { textStyle } from "@/lib/typography";
import { cn } from "@/lib/utils";

/**
 * The effective runtime configuration, read from the running agent.
 *
 * Read-only rather than a form: these come from Terraform and the deployment
 * environment, so an editable field here would be a lie. Unlike the goal and
 * memory cards, everything is shown, including empty values, since an unset
 * id is exactly what someone comes to this card to find out.
 *
 * Grouped in a curated order rather than listed as the payload arrives, with
 * an "Other" bucket that cannot drop a key the grouping has never heard of,
 * and the raw record underneath for the question the labels do not answer.
 */
export function EffectiveConfigCard() {
  const { data, isLoading, error } = useQuery(effectiveConfigQuery());

  if (isLoading) {
    return (
      <section className="rounded-lg border bg-card p-4">
        <Loading label="Loading configuration…" />
      </section>
    );
  }

  const failure = error ? (error as Error).message : data?.error;
  if (failure) {
    return (
      <section className="flex flex-col gap-2 rounded-lg border border-amber-500/40 bg-card p-4">
        <h2 className={textStyle.sectionTitle}>Agent configuration</h2>
        <p
          className={cn(
            textStyle.meta,
            "flex items-center gap-1.5 text-amber-500",
          )}
        >
          <AlertCircle className="h-4 w-4 shrink-0" />
          Couldn&apos;t read the configuration: {failure}
        </p>
      </section>
    );
  }

  const config = data?.config ?? {};
  const groups = groupConfig(config);

  return (
    <section className="flex flex-col gap-3 rounded-lg border bg-card p-4">
      <div className="flex flex-col gap-1">
        <h2 className={textStyle.sectionTitle}>Agent configuration</h2>
        <p className={textStyle.meta}>
          The effective settings this deployment is running with. Read-only:
          these come from the deployment, not from here.
        </p>
      </div>

      {groups.length === 0 ? (
        <p className={textStyle.meta}>The agent returned no configuration.</p>
      ) : (
        <>
          {groups.map((group) => (
            <div key={group.title} className="flex flex-col gap-1.5">
              <h3 className={textStyle.label}>{group.title}</h3>
              <dl className="grid grid-cols-1 gap-x-6 gap-y-1.5 sm:grid-cols-2">
                {group.entries.map(([key, value]) => (
                  <ConfigRow key={key} name={key} value={value} />
                ))}
              </dl>
            </div>
          ))}
          <RawJson label="Raw configuration" value={config} />
        </>
      )}
    </section>
  );
}

function ConfigRow({ name, value }: { name: string; value: unknown }) {
  const rendered = configValue(value);
  return (
    <div className="flex items-baseline justify-between gap-3 border-b border-border/40 py-1">
      <dt className={cn(textStyle.label, "shrink-0")}>{configLabel(name)}</dt>
      {rendered.kind === "pills" ? (
        <dd className="flex flex-wrap justify-end gap-1">
          {rendered.items.map((item) => (
            <span
              key={item}
              className={cn(
                textStyle.code,
                "rounded-full border bg-muted/40 px-2 py-px",
              )}
            >
              {item}
            </span>
          ))}
        </dd>
      ) : (
        <dd
          className={cn(textStyle.code, "truncate text-right")}
          title={rendered.text}
        >
          {rendered.text}
        </dd>
      )}
    </div>
  );
}
