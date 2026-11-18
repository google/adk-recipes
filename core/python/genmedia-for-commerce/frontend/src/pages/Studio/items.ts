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

// The user's image picks as studio actions (`{label, ref}` items, studio/patches.py), as the MCP
// App panel sends them; the thumbnails stay on the client. A pick, a removal or a clear shows at
// once while its action saves: the answer replaces it, or the server's state if it fails.
import { uploadStudioImage, type StudioItem } from "../../api/studio";
import { errorMessage } from "../../shared/files";
import { cleanItem, lean, MAX_ITEMS, type StudioView } from "./model";
import { uploadFile } from "./shrink";
import type { StudioStore } from "./store";

export type SlotField = "person" | "reference_model";
export type ListField = "product" | "spin";
export type UploadField = SlotField | ListField;
/** The view's list of each list field. */
const LISTS = { product: "products", spin: "spin" } as const;
/** Whether `field` is a list field (`in` alone does not narrow its type). */
const isList = (field: UploadField): field is ListField => field in LISTS;

/** `list` without its first `field`. */
function withoutOne<T>(list: T[], field: T): T[] {
  const i = list.indexOf(field);
  return i < 0 ? list : [...list.slice(0, i), ...list.slice(i + 1)];
}

/** At most `n` calls of `fn` at a time: a call that ends hands its slot to the next one waiting. */
function limited<A extends unknown[], R>(
  n: number,
  fn: (...a: A) => Promise<R>,
): (...a: A) => Promise<R> {
  let active = 0;
  const waiting: (() => void)[] = [];
  return async (...a) => {
    if (active < n) active++;
    else
      await new Promise<void>((resolve) => {
        waiting.push(resolve);
      });
    try {
      return await fn(...a);
    } finally {
      const next = waiting.shift();
      if (next) next();
      else active--;
    }
  };
}

/** One upload; up to 4 at once. */
const send = limited(4, async (studioId: string, file: File) =>
  uploadStudioImage(studioId, await uploadFile(file)),
);

export class ItemCommands {
  private readonly store: StudioStore;

  constructor(store: StudioStore) {
    this.store = store;
  }

  /** Show a pick before its action is answered (not before the saved studio was read: nothing is sent then). */
  private show(change: Partial<StudioView>): void {
    const { ready, view } = this.store.state;
    if (ready) this.store.set({ view: { ...view, ...change } });
  }

  /** An image added to a list, shown at the end. */
  private showAdded(field: ListField, item: StudioItem): void {
    const list = LISTS[field];
    this.show({ [list]: [...this.store.state.view[list], item] });
  }

  setSlot = (field: SlotField, item: StudioItem, source: string) => {
    this.store.remember(item);
    this.show({ [field]: item });
    return this.store.act({ set: { field, item: lean(item) }, source });
  };

  clearField = (field: SlotField | ListField) => {
    this.show(isList(field) ? { [LISTS[field]]: [] } : { [field]: null });
    return this.store.act({ clear: { field } });
  };

  removeAt = (field: ListField, index: number) => {
    const list = LISTS[field];
    const shown = this.store.state.view[list];
    // Saved by ref: the shown list can differ from the saved one.
    const ref = shown[index]?.ref ?? "";
    this.show({ [list]: shown.filter((_, i) => i !== index) });
    const saved = this.store.copies.get(ref) ?? ref;
    return this.store.act({
      remove: { field, index, ...(ref ? { ref: saved } : {}) },
    });
  };

  /** Whether a product (or the studio's copy of a catalogue product) is in the studio. */
  hasProduct = (ref: string) => {
    const copy = this.store.copies.get(ref);
    return this.store.state.view.products.some(
      (p) => p.ref === ref || p.ref === copy,
    );
  };

  /** Add a product unless it is in the studio already or the studio has 8. */
  addProduct = (item: StudioItem, source: string) => {
    if (
      this.store.state.view.products.length >= MAX_ITEMS ||
      this.hasProduct(item.ref)
    )
      return;
    this.store.remember(item);
    this.showAdded("product", item);
    void this.store.act({
      add: { field: "product", item: lean(item), replace_index: -1 },
      source,
    });
  };

  setSpin = (items: StudioItem[], source: string) => {
    items.forEach((item) => {
      this.store.remember(item);
    });
    this.show({ spin: items.slice(0, MAX_ITEMS) });
    void this.store.act({
      set: { field: "spin", items: items.map(lean) },
      source,
    });
  };

  /** Upload images (the originals; shrunk only over the server's limit) into a slot or a list, up to 4 at once,
   * each added (sent as the user's upload) in the order picked; `uploading` names the field of each one in flight. */
  upload = async (field: UploadField, files: File[]) => {
    const { store } = this;
    const { epoch } = store;
    const { studioId } = store.state;
    const picked = files.slice(0, MAX_ITEMS);
    const fields = picked.map(() => field);
    store.set({ uploading: [...store.state.uploading, ...fields] });
    const sent = picked.map((file) => send(studioId, file));
    for (const p of sent) p.catch(() => undefined); // reported in its turn
    for (const pending of sent) {
      try {
        const up = await pending;
        if (epoch !== store.epoch) return;
        const item = cleanItem({
          label: up.label,
          ref: up.ref,
          thumb: up.thumb,
        });
        if (!item) throw new Error("no image saved");
        store.remember(item);
        const meta = { source: "upload", description: up.description ?? "" };
        const list =
          field === "spin" ? store.state.view.spin : store.state.view.products;
        if (field !== "product" && field !== "spin") {
          this.show({ [field]: item });
          await store.act({ set: { field, item: lean(item) }, ...meta });
        } else if (list.length < MAX_ITEMS) {
          this.showAdded(field, item);
          await store.act({
            add: { field, item: lean(item), replace_index: -1 },
            ...meta,
          });
        }
      } catch (e) {
        if (epoch === store.epoch)
          store.set({
            jobError: `Upload failed: ${errorMessage(e)}`,
            retry: null,
          });
      } finally {
        if (epoch === store.epoch)
          store.set({ uploading: withoutOne(store.state.uploading, field) });
      }
    }
  };
}
