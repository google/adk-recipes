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

import {
  useCallback,
  useEffect,
  useRef,
  useState,
  type FormEvent,
} from "react";
import { searchStudioCatalog, type StudioItem } from "../../api/studio";
import { SearchIcon, SpinnerIcon } from "../../components/icons";
import { errorMessage } from "../../shared/files";
import { cleanItem } from "./model";
import TileGrid from "./TileGrid";

const AUDIENCES = { women: "Women", men: "Men", unisex: "Unisex" };

type Shown = { query: string; items: StudioItem[] };

type ProductPickerProps = {
  studioId: string;
  onUse: (item: StudioItem) => void;
  isPicked?: (item: StudioItem) => boolean;
  full?: boolean;
};

const Heading = ({ children }: { children: string }) => (
  <h4 className="text-caption text-gm-text-tertiary">{children}</h4>
);

/** The catalogue's products: the featured ones, or a search through the studio (the agent sees what was searched). */
export default function ProductPicker({
  studioId,
  onUse,
  isPicked,
  full,
}: ProductPickerProps) {
  const [query, setQuery] = useState("");
  const [audience, setAudience] = useState("");
  const [shown, setShown] = useState<Shown | null>(null);
  const [busy, setBusy] = useState(true);
  const [error, setError] = useState("");
  const latest = useRef(0);

  // An empty query lists the featured products; only the latest answer is shown.
  const answer = useCallback(
    async (q: string, aud: string) => {
      const call = ++latest.current;
      try {
        const found = await searchStudioCatalog(studioId, q, aud);
        if (call !== latest.current) return;
        setShown({
          query: q,
          items: (found.items ?? [])
            .map(cleanItem)
            .filter((x): x is StudioItem => !!x),
        });
        setError(found.error ?? "");
      } catch (e) {
        if (call === latest.current) setError(errorMessage(e));
      } finally {
        if (call === latest.current) setBusy(false);
      }
    },
    [studioId],
  );

  useEffect(() => {
    // Opening the picker loads the featured products; state is set when they arrive.
    void answer("", "");
  }, [answer]);

  const load = (q: string, aud: string) => {
    setBusy(true);
    setError("");
    void answer(q, aud);
  };
  const search = (event: FormEvent) => {
    event.preventDefault();
    if (!busy) load(query.trim(), audience);
  };
  const pickAudience = (value: string) => {
    const next = audience === value ? "" : value;
    setAudience(next);
    load(shown?.query ?? "", next);
  };
  const items = shown?.items ?? [];
  const searching = !!shown?.query;

  return (
    <div className="flex flex-col gap-4">
      <form onSubmit={search} className="flex gap-2">
        <div className="relative flex-1">
          <SearchIcon className="absolute left-3 top-1/2 -translate-y-1/2 text-gm-text-tertiary" />
          <input
            type="search"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            aria-label="Search the catalogue"
            // biome-ignore lint/a11y/noAutofocus: the picker opens in a dialog to search the catalogue
            autoFocus
            placeholder="Search the catalogue, e.g. light blue linen shirt"
            className="input-glass w-full !pl-9 !py-2.5 text-sm"
          />
        </div>
        <button
          type="submit"
          disabled={busy || query.trim() === (shown?.query ?? "")}
          className="btn-primary !px-5 !py-2.5 text-button inline-flex items-center gap-2"
        >
          {busy && <SpinnerIcon size={16} />} Search
        </button>
      </form>
      <div className="flex gap-2">
        {Object.entries(AUDIENCES).map(([value, label]) => (
          <button
            key={value}
            type="button"
            aria-pressed={audience === value}
            onClick={() => pickAudience(value)}
            className={`px-3 py-1 rounded-pill text-xs border transition-all ${audience === value ? "bg-gm-accent text-gm-bg border-gm-accent" : "border-white/15"}`}
          >
            {label}
          </button>
        ))}
      </div>
      {error && (
        <p role="alert" className="text-sm text-gm-error">
          {error}
        </p>
      )}
      <Heading>
        {searching ? `Results for “${shown?.query}”` : "Featured"}
      </Heading>
      {!shown && busy && <SpinnerIcon className="text-gm-accent mx-auto" />}
      {shown && !items.length && !busy && (
        <p className="text-sm text-gm-text-tertiary">No products match.</p>
      )}
      <TileGrid
        kind="product"
        full={full}
        tiles={items.map((item) => ({
          key: item.ref,
          label: item.label,
          thumb: item.thumb ?? "",
        }))}
        onPick={(i) => onUse(items[i])}
        isPicked={isPicked && ((i) => isPicked(items[i]))}
      />
    </div>
  );
}
