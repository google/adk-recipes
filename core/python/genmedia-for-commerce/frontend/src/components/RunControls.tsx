/**
 * Copyright 2026 Google LLC
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     https://www.apache.org/licenses/LICENSE-2.0
 *
 * Unless required by applicable law or agreed to in writing, software
 * distributed under the License is distributed on an "AS IS" BASIS,
 * WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 * See the License for the specific language governing permissions and
 * limitations under the License.
 */

import { useId, type ReactNode } from "react";
import { SparkleIcon } from "./icons";

/** Placeholder for the results column before the first run: a title, what to do, and optional steps. */
export function EmptyResults({
  title,
  text,
  children,
}: {
  title: string;
  text: string;
  children?: ReactNode;
}) {
  return (
    <div className="relative overflow-hidden rounded-2xl border border-dashed border-white/[0.12] bg-white/[0.015] min-h-[360px] flex flex-col items-center justify-center gap-4 p-8 text-center">
      <div
        aria-hidden="true"
        className="absolute inset-0 bg-radial-glow opacity-40 pointer-events-none"
      />
      <span className="relative w-14 h-14 rounded-2xl bg-gm-accent/10 ring-1 ring-inset ring-gm-accent/25 text-gm-accent flex items-center justify-center shadow-accent-glow">
        <SparkleIcon size={24} />
      </span>
      <div className="relative flex flex-col gap-1 max-w-md">
        <h2 className="text-h2">{title}</h2>
        <p className="text-body text-gm-text-secondary">{text}</p>
      </div>
      {children && <div className="relative">{children}</div>}
    </div>
  );
}

/** Labelled text input or textarea for the page forms. */
export function TextField({
  id,
  label,
  value,
  onChange,
  placeholder,
  rows,
}: {
  id: string;
  label: string;
  value: string;
  onChange: (value: string) => void;
  placeholder?: string;
  rows?: number;
}) {
  const className = "input-glass w-full !py-2.5 !px-3 text-sm";
  return (
    <div className="flex flex-col gap-1.5">
      <label htmlFor={id} className="text-sm font-medium text-gm-text-primary">
        {label}
      </label>
      {rows ? (
        <textarea
          id={id}
          rows={rows}
          value={value}
          onChange={(e) => onChange(e.target.value)}
          placeholder={placeholder}
          className={className}
        />
      ) : (
        <input
          id={id}
          value={value}
          onChange={(e) => onChange(e.target.value)}
          placeholder={placeholder}
          className={className}
        />
      )}
    </div>
  );
}

type SelectProps = {
  label: string;
  value: string | number;
  options: readonly (string | number)[];
  /** Display text per option; the value is shown when absent. */
  labels?: Record<string, string>;
  onChange: (value: string) => void;
};

/** Labelled select of the run options panel. */
export function Select({
  label,
  value,
  options,
  labels,
  onChange,
}: SelectProps) {
  const id = useId();
  return (
    <div className="flex flex-col gap-1.5">
      <label htmlFor={id} className="text-caption text-gm-text-secondary">
        {label}
      </label>
      <select
        id={id}
        value={value}
        onChange={(e) => onChange(e.target.value)}
        className="input-glass !py-2 !pl-3 text-sm"
      >
        {options.map((option) => (
          <option key={option} value={option}>
            {labels?.[option] ?? option}
          </option>
        ))}
      </select>
    </div>
  );
}

type SwitchProps = {
  label: string;
  hint: string;
  checked: boolean;
  onChange: (checked: boolean) => void;
};

/** An on/off option with its explanation. */
export function Switch({ label, hint, checked, onChange }: SwitchProps) {
  return (
    <div className="flex items-start gap-3 rounded-xl bg-white/[0.02] border border-white/[0.06] p-3">
      <button
        type="button"
        role="switch"
        aria-label={label}
        aria-checked={checked}
        onClick={() => onChange(!checked)}
        className={`shrink-0 mt-0.5 w-9 h-5 rounded-full relative transition-all ${checked ? "bg-gm-accent" : "bg-white/20"}`}
      >
        <span
          className={`absolute top-0.5 w-4 h-4 rounded-full bg-white shadow-level-1 transition-all ${checked ? "left-[18px]" : "left-0.5"}`}
        />
      </button>
      <div className="flex flex-col gap-0.5">
        <span className="text-sm font-medium">{label}</span>
        <p className="text-caption font-normal text-gm-text-tertiary">{hint}</p>
      </div>
    </div>
  );
}
