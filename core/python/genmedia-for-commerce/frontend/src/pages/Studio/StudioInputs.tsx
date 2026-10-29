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

// The image fields of the studio. Each pick is sent as an action; the cards show the
// server's state back.
import { useState, type ReactNode } from "react";
import type { GalleryProduct } from "../../api/studio";
import {
  GridIcon,
  SearchIcon,
  SpinIcon,
  SpinnerIcon,
  TagIcon,
  UserIcon,
} from "../../components/icons";
import Modal from "../../components/Modal";
import ItemSlot from "./ItemSlot";
import { MAX_ITEMS } from "./model";
import ProductPicker from "./ProductPicker";
import { LookOptions } from "./StudioSettings";
import TileGrid from "./TileGrid";
import type { Studio } from "./useStudio";

type Picker = "gallery" | "products" | "spin_sets" | null;
type FieldProps = { studio: Studio };

/** The gallery once loaded: a spinner until then, or why it failed. */
function GalleryState({
  studio,
  children,
}: FieldProps & { children: ReactNode }) {
  if (studio.galleryError)
    return (
      <p role="alert" className="text-sm text-gm-error">
        {studio.galleryError}
      </p>
    );
  if (!studio.gallery)
    return <SpinnerIcon className="text-gm-accent mx-auto" />;
  return children;
}

const SourceButton = ({
  onClick,
  children,
}: {
  onClick: () => void;
  children: ReactNode;
}) => (
  <button
    type="button"
    className="glass-button h-8 px-3 rounded-lg text-sm flex items-center gap-1.5"
    onClick={onClick}
  >
    {children}
  </button>
);

/** Person (an upload or a gallery model) or reference model (the user's own photo of a model wearing the look: upload only), with its options. */
export function ModelField({
  studio,
  field,
}: FieldProps & { field: "person" | "reference_model" }) {
  const [open, setOpen] = useState<Picker>(null);
  const item = studio.view[field];
  const person = field === "person";
  const models = studio.gallery?.models ?? [];
  const label = person ? "Person" : "Reference model (optional)";

  return (
    <>
      <ItemSlot
        label={label}
        icon={UserIcon}
        max={1}
        items={item ? [item] : []}
        onRemove={() => studio.clearField(field)}
        onFiles={(files) => studio.upload(field, files)}
        uploading={studio.uploading.includes(field)}
        footer={!person && item && <LookOptions studio={studio} />}
        hint={
          person
            ? "A photo of the person, or a model from the gallery."
            : "Upload your own photo of a model already wearing the look; its parts are used when you have no worn photo of an item."
        }
      >
        {person && (
          <SourceButton onClick={() => setOpen("gallery")}>
            <GridIcon size={14} /> Gallery
          </SourceButton>
        )}
      </ItemSlot>
      {person && (
        <Modal
          isOpen={open === "gallery"}
          onClose={() => setOpen(null)}
          title="Model gallery"
        >
          <GalleryState studio={studio}>
            <TileGrid
              kind="portrait"
              tiles={models.map((m) => ({
                key: m.ref,
                label: m.label,
                thumb: m.thumb ?? "",
              }))}
              onPick={(i) => {
                setOpen(null);
                void studio.setSlot(field, models[i], "gallery");
              }}
            />
          </GalleryState>
        </Modal>
      )}
    </>
  );
}

/** Try-on products: uploads and catalogue products (featured, or searched), up to 8, each once. */
export function ProductsField({ studio }: FieldProps) {
  const [open, setOpen] = useState<Picker>(null);
  const { products } = studio.view;
  const full = products.length >= MAX_ITEMS;

  return (
    <>
      <ItemSlot
        label="Products"
        icon={TagIcon}
        max={MAX_ITEMS}
        items={products}
        hint="Several photos of one product, or one photo per product for an outfit."
        onRemove={(i) => studio.removeAt("product", i)}
        onClear={() => studio.clearField("product")}
        onFiles={(files) => studio.upload("product", files)}
        uploading={studio.uploading.includes("product")}
      >
        <SourceButton onClick={() => setOpen("products")}>
          <SearchIcon size={14} /> Products
        </SourceButton>
      </ItemSlot>
      <Modal
        isOpen={open === "products"}
        onClose={() => setOpen(null)}
        title="Products"
      >
        <ProductPicker
          studioId={studio.studioId}
          full={full}
          onUse={(item) => studio.addProduct(item, "catalog")}
          isPicked={(item) => studio.hasProduct(item.ref)}
        />
      </Modal>
    </>
  );
}

/** 360° Spin photos: uploads or every view of a catalogue spin set (a catalogue product has one photo, too few to
 * spin). One photo is accepted, with a warning. */
export function SpinField({ studio }: FieldProps) {
  const [open, setOpen] = useState<Picker>(null);
  const sets = studio.gallery?.spin ?? [];
  const pickSet = (p: GalleryProduct) => {
    setOpen(null);
    // Each view its own thumbnail; one the gallery has none for is previewed once saved.
    studio.setSpin(
      p.refs.map((ref, i) => ({
        label: `${p.label} ${i + 1}`,
        ref,
        thumb: p.thumbs?.[i] ?? "",
      })),
      "gallery",
    );
  };
  const onePhoto = studio.view.spin.length === 1 && (
    <p role="status" className="text-caption text-gm-warning">
      One photo works, but the spin will invent the sides and back: add photos
      from other angles.
    </p>
  );

  return (
    <>
      <ItemSlot
        label="Product photos"
        icon={SpinIcon}
        max={MAX_ITEMS}
        items={studio.view.spin}
        hint="Use several photos of the same product from different sides."
        onRemove={(i) => studio.removeAt("spin", i)}
        onClear={() => studio.clearField("spin")}
        onFiles={(files) => studio.upload("spin", files)}
        uploading={studio.uploading.includes("spin")}
        footer={onePhoto}
      >
        <SourceButton onClick={() => setOpen("spin_sets")}>
          <GridIcon size={14} /> Spin sets
        </SourceButton>
      </ItemSlot>
      <Modal
        isOpen={open === "spin_sets"}
        onClose={() => setOpen(null)}
        title="Spin sets"
      >
        <GalleryState studio={studio}>
          <p className="text-caption text-gm-text-secondary mb-3">
            Catalogue products photographed from several sides.
          </p>
          <TileGrid
            kind="product"
            tiles={sets.map((s) => ({
              key: s.refs[0] ?? s.label,
              label: s.label,
              thumb: s.thumb,
              meta: `${s.refs.length} views`,
            }))}
            onPick={(i) => pickSet(sets[i])}
          />
        </GalleryState>
      </Modal>
    </>
  );
}
