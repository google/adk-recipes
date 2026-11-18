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

import { act, renderHook, waitFor } from "@testing-library/react";
import { http, HttpResponse } from "msw";
import { beforeEach, describe, expect, it } from "vitest";
import { sendStudioAction, type StudioAction } from "../../api/studio";
import {
  FEATURED,
  resetStudioMock,
  STUDIO_GALLERY,
} from "../../test/studioMock";
import { server, setupMockBackend, stubLocalStorage } from "../../test/utils";
import { useStudio } from "./useStudio";

setupMockBackend();

const ID = "studio123abc";
const [EMMA] = STUDIO_GALLERY.models;
const [JEANS, JACKET] = FEATURED;

/** A studio with a person and two products, opened in this browser. */
async function renderStudio() {
  await sendStudioAction(ID, { set: { field: "person", item: EMMA } });
  for (const item of [JEANS, JACKET])
    await sendStudioAction(ID, {
      add: { field: "product", item, replace_index: -1 },
    });
  const view = renderHook(() => useStudio());
  await waitFor(() => expect(view.result.current.ready).toBe(true));
  return view;
}

const labels = (items: { label: string }[]) => items.map((i) => i.label);

beforeEach(() => {
  resetStudioMock();
  stubLocalStorage();
  localStorage.setItem("genmedia.studioId", ID);
});

describe("Upload", () => {
  it("uploads several files at once and adds them in the order picked", async () => {
    let release!: () => void;
    const gate = new Promise<void>((resolve) => {
      release = resolve;
    });
    const started: string[] = [];
    const added: string[] = [];
    const { result } = await renderStudio();
    server.use(
      // The PNG (picked first) answers only once the JPEG's upload started.
      http.post("/api/studio/:id/uploads", async ({ params, request }) => {
        const type = /Content-Type: (image\/\w+)/.exec(await request.text());
        const name = type?.[1] === "image/png" ? "first" : "second";
        started.push(name);
        if (name === "first") await gate;
        const ref = `gs://b/runs/2026-10-08/studio-${params.id}/inputs/upload-${name}.png`;
        return HttpResponse.json({ ref, label: name, thumb: "" });
      }),
      http.post("/api/studio/:id/actions", async ({ request }) => {
        const { event } = (await request.clone().json()) as {
          event: StudioAction;
        };
        const add = event.add as { item: { label: string } } | undefined;
        if (add) added.push(add.item.label);
      }),
    );
    const first = new File(["a"], "a.png", { type: "image/png" });
    const second = new File(["b"], "b.jpg", { type: "image/jpeg" });

    let done!: Promise<void>;
    act(() => {
      done = result.current.upload("product", [first, second]);
    });
    await waitFor(() => expect(started).toEqual(["first", "second"]));
    expect(result.current.uploading).toEqual(["product", "product"]);
    expect(added).toEqual([]); // the second waits for the first
    release();
    await act(() => done);

    expect(added).toEqual(["first", "second"]);
    expect(result.current.uploading).toEqual([]);
  });
});

describe("Remove and Clear", () => {
  it("show at once, before their actions are answered", async () => {
    let release!: () => void;
    const gate = new Promise<void>((resolve) => {
      release = resolve;
    });
    const { result } = await renderStudio();
    server.use(
      http.post("/api/studio/:id/actions", async () => {
        await gate; // then the mock answers
      }),
    );

    let saved!: Promise<unknown>;
    act(() => {
      void result.current.removeAt("product", 0);
      saved = result.current.clearField("person");
    });

    expect(labels(result.current.view.products)).toEqual([JACKET.label]);
    expect(result.current.view.person).toBeNull();
    expect(result.current.saving).toBe(true);
    release();
    await act(() => saved);
    await waitFor(() => expect(result.current.saving).toBe(false));
    expect(labels(result.current.view.products)).toEqual([JACKET.label]);
    expect(result.current.view.person).toBeNull();
  });

  it("show the server's state again when the action fails", async () => {
    const { result } = await renderStudio();
    server.use(
      http.post("/api/studio/:id/actions", () =>
        HttpResponse.json(
          { detail: "Storage is unavailable; try again." },
          { status: 503 },
        ),
      ),
    );

    let saved!: Promise<unknown>;
    act(() => {
      saved = result.current.clearField("product");
    });
    expect(result.current.view.products).toEqual([]);
    await act(() => saved);

    expect(result.current.notice).toBe(
      "Not saved: Storage is unavailable; try again.",
    );
    expect(labels(result.current.view.products)).toEqual([
      JEANS.label,
      JACKET.label,
    ]);
  });
});
