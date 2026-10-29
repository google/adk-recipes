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

import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import { http, HttpResponse } from "msw";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { sendStudioAction, type StudioAction } from "../../api/studio";
import {
  FEATURED,
  resetStudioMock,
  STUDIO_GALLERY,
} from "../../test/studioMock";
import {
  imageFile,
  renderAt,
  server,
  setupMockBackend,
  stubLocalStorage,
} from "../../test/utils";
import { POLLING } from "./jobs";
import Studio from ".";

setupMockBackend();

const ID = "studio123abc";
const [EMMA] = STUDIO_GALLERY.models;
const JEANS = { label: "Blue jeans", ref: FEATURED[0].ref };
const interval = POLLING.intervalMs;

function recordActions() {
  const events: StudioAction[] = [];
  server.use(
    http.post("/api/studio/:id/actions", async ({ request }) => {
      events.push(
        ((await request.clone().json()) as { event: StudioAction }).event,
      );
    }),
  );
  return events;
}

async function renderStudio(...saved: StudioAction[]) {
  for (const action of saved) await sendStudioAction(ID, action);
  const view = renderAt("/", { "/": <Studio /> });
  await waitFor(() =>
    expect(
      screen.getByRole("region", { name: "Inputs" }).getAttribute("aria-busy"),
    ).toBe("false"),
  );
  return view;
}

const tab = (name: string) => screen.getByRole("tab", { name });
const card = (title: string) =>
  screen
    .getByRole("heading", { name: title })
    .closest(".glass-panel") as HTMLElement;
const chat = () => screen.getByRole("complementary", { name: "Studio agent" });

beforeEach(() => {
  resetStudioMock();
  stubLocalStorage();
  localStorage.setItem("genmedia.studioId", ID);
  POLLING.intervalMs = 5;
});
afterEach(() => {
  POLLING.intervalMs = interval;
});

describe("Studio page", () => {
  it("opens a new studio on Photo Try-On with suggestions in the chat", async () => {
    await renderStudio();

    expect(tab("Photo Try-On").getAttribute("aria-selected")).toBe("true");
    expect(
      await within(chat()).findByRole("button", {
        name: "Make a 360 spin of a sneaker",
      }),
    ).toBeTruthy();
    expect(screen.getByText("Choose a person first.")).toBeTruthy();
  });

  it("renders the saved studio", async () => {
    await renderStudio(
      { set: { field: "person", item: EMMA } },
      { add: { field: "product", item: JEANS, replace_index: -1 } },
      { settings: { scenario: "a beach at sunset" }, page: "tryon" },
    );

    expect(await within(card("Person")).findByAltText("Emma")).toBeTruthy();
    expect(within(card("Products")).getByAltText("Blue jeans")).toBeTruthy();
    expect((screen.getByLabelText("Scenario") as HTMLInputElement).value).toBe(
      "a beach at sunset",
    );
    expect(
      (
        screen.getByRole("button", {
          name: "Generate Photo Try-On",
        }) as HTMLButtonElement
      ).disabled,
    ).toBe(false);
  });

  it("shows no suggestion and sends nothing until the conversation is loaded", async () => {
    let release!: () => void;
    const gate = new Promise<void>((resolve) => {
      release = resolve;
    });
    server.use(
      http.get("/api/studio/:id/messages", async () => {
        await gate;
      }),
    );
    await renderStudio();

    expect(
      within(chat()).queryByRole("button", {
        name: "Make a 360 spin of a sneaker",
      }),
    ).toBeNull();
    expect(
      (
        within(chat()).getByLabelText(
          "Message the studio agent",
        ) as HTMLTextAreaElement
      ).disabled,
    ).toBe(true);
    release();
    expect(
      await within(chat()).findByRole("button", {
        name: "Make a 360 spin of a sneaker",
      }),
    ).toBeTruthy();
    expect(
      (
        within(chat()).getByLabelText(
          "Message the studio agent",
        ) as HTMLTextAreaElement
      ).disabled,
    ).toBe(false);
  });

  it("sends a message with Enter and shows the reply and the studio the agent set up", async () => {
    await renderStudio();
    const input = within(chat()).getByLabelText("Message the studio agent");
    await waitFor(() =>
      expect((input as HTMLTextAreaElement).disabled).toBe(false),
    );

    fireEvent.change(input, {
      target: { value: "Make a 360 spin of a sneaker" },
    });
    fireEvent.keyDown(input, { key: "Enter" });

    expect(
      within(chat()).getByText("Make a 360 spin of a sneaker"),
    ).toBeTruthy();
    expect(within(chat()).getByText("Working…")).toBeTruthy();
    expect(await within(chat()).findByText(/I opened 360° Spin/)).toBeTruthy();
    await waitFor(() =>
      expect(tab("360° Spin").getAttribute("aria-selected")).toBe("true"),
    );
    expect(
      within(card("Product photos")).getAllByRole("listitem"),
    ).toHaveLength(2);
    expect((input as HTMLTextAreaElement).value).toBe("");
  });

  it("sends a gallery model pick as a set action", async () => {
    const events = recordActions();
    await renderStudio();

    fireEvent.click(
      within(card("Person")).getByRole("button", { name: "Gallery" }),
    );
    fireEvent.click(
      within(screen.getByRole("dialog", { name: "Model gallery" })).getByRole(
        "button",
        { name: "Emma" },
      ),
    );

    await waitFor(() =>
      expect(events).toEqual([
        {
          set: { field: "person", item: { label: "Emma", ref: EMMA.ref } },
          source: "gallery",
        },
      ]),
    );
    expect(await within(card("Person")).findByAltText("Emma")).toBeTruthy();
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("picks products from the featured ones or a search, in one picker", async () => {
    const events = recordActions();
    await renderStudio();

    const products = card("Products");
    fireEvent.click(within(products).getByRole("button", { name: "Products" }));
    const picker = screen.getByRole("dialog", { name: "Products" });
    expect(
      within(picker).getByRole("heading", { name: "Featured" }),
    ).toBeTruthy();
    fireEvent.click(
      await within(picker).findByRole("button", { name: "Blue jeans" }),
    );
    await waitFor(() =>
      expect(events).toEqual([
        {
          add: {
            field: "product",
            item: { label: "Blue jeans", ref: FEATURED[0].ref },
            replace_index: -1,
          },
          source: "catalog",
        },
      ]),
    );

    fireEvent.change(within(picker).getByLabelText("Search the catalogue"), {
      target: { value: "red" },
    });
    fireEvent.click(within(picker).getByRole("button", { name: "Search" }));
    expect(
      await within(picker).findByRole("heading", { name: "Results for “red”" }),
    ).toBeTruthy();
    expect(
      within(picker).queryByRole("button", { name: "Blue jeans" }),
    ).toBeNull();
    fireEvent.click(
      within(picker).getByRole("button", { name: "Red wool scarf" }),
    );
    await waitFor(() => expect(events).toHaveLength(2));
    expect(events[1]).toMatchObject({
      add: { item: { label: "Red wool scarf", ref: "catalog:p2" } },
      source: "catalog",
    });
    await waitFor(() =>
      expect(within(products).getAllByRole("listitem")).toHaveLength(2),
    );
    expect(
      within(picker)
        .getByRole("button", { name: "Red wool scarf" })
        .getAttribute("aria-pressed"),
    ).toBe("true");
  });

  it("offers only the spin sets, not single catalogue photos, in the spin picker", async () => {
    await renderStudio({ capability: "spin" });
    const events = recordActions();

    const photos = card("Product photos");
    expect(
      within(photos).getByText(
        "Use several photos of the same product from different sides.",
      ),
    ).toBeTruthy();
    expect(
      within(photos).queryByRole("button", { name: "Products" }),
    ).toBeNull();
    fireEvent.click(within(photos).getByRole("button", { name: "Spin sets" }));
    const picker = screen.getByRole("dialog", { name: "Spin sets" });
    expect(within(picker).queryByLabelText("Search the catalogue")).toBeNull();
    fireEvent.click(
      await within(picker).findByRole("button", { name: "White sneaker" }),
    );

    const [set] = STUDIO_GALLERY.spin;
    await waitFor(() =>
      expect(events).toEqual([
        {
          set: {
            field: "spin",
            items: set.refs.map((ref, i) => ({
              label: `White sneaker ${i + 1}`,
              ref,
            })),
          },
          source: "gallery",
        },
      ]),
    );
    expect(screen.queryByRole("dialog")).toBeNull();
    // Each view shows its own image, not the set's first one.
    const views = () =>
      within(photos)
        .getAllByRole("img")
        .map((img) => img.getAttribute("src"));
    await waitFor(() => expect(views()).toEqual(set.thumbs));
  });

  it("accepts one spin photo with a warning to add other angles", async () => {
    await renderStudio({ capability: "spin" });
    const photos = card("Product photos");
    expect(within(photos).queryByRole("status")).toBeNull();

    fireEvent.change(screen.getByLabelText("Upload Product photos"), {
      target: { files: [imageFile("front.png")] },
    });

    expect(await within(photos).findByRole("status")).toHaveProperty(
      "textContent",
      expect.stringMatching(/invent the sides and back/),
    );
    fireEvent.change(screen.getByLabelText("Upload Product photos"), {
      target: { files: [imageFile("side.png")] },
    });
    await waitFor(() =>
      expect(within(photos).getAllByRole("listitem")).toHaveLength(2),
    );
    expect(within(photos).queryByRole("status")).toBeNull();
  });

  it("uploads a product photo and adds it as the user's upload", async () => {
    const events = recordActions();
    await renderStudio();

    fireEvent.change(screen.getByLabelText("Upload Products"), {
      target: { files: [imageFile("jacket.png")] },
    });

    await waitFor(() => expect(events).toHaveLength(1));
    expect(events[0]).toMatchObject({
      add: { field: "product", replace_index: -1 },
      source: "upload",
      description: expect.any(String),
    });
    await waitFor(() =>
      expect(within(card("Products")).getAllByRole("listitem")).toHaveLength(1),
    );
  });

  it("sends the original photo and shows the upload in its card until it is saved", async () => {
    let release!: () => void;
    const gate = new Promise<void>((resolve) => {
      release = resolve;
    });
    const bodies: string[] = [];
    server.use(
      http.post("/api/studio/:id/uploads", async ({ request }) => {
        bodies.push(await request.clone().text());
        await gate;
      }),
    );
    await renderStudio();
    const photo = new File(["png"], "jacket.png", { type: "image/png" });

    fireEvent.change(screen.getByLabelText("Upload Products"), {
      target: { files: [photo] },
    });

    expect(
      await within(card("Products")).findByText("Uploading…"),
    ).toBeTruthy();
    expect(within(card("Person")).queryByText("Uploading…")).toBeNull();
    expect(
      screen
        .getAllByRole("status")
        .some((s) => s.textContent?.includes("Uploading…")),
    ).toBe(true);
    release();
    await waitFor(() =>
      expect(within(card("Products")).getAllByRole("listitem")).toHaveLength(1),
    );
    expect(within(card("Products")).queryByText("Uploading…")).toBeNull();
    // The PNG itself, not a JPEG copy (jsdom's multipart body keeps the type, not the name or bytes).
    expect(bodies).toHaveLength(1);
    expect(bodies[0]).toContain("Content-Type: image/png");
  });

  it("generates and shows the results, with no way to animate one", async () => {
    await renderStudio(
      { set: { field: "person", item: EMMA } },
      { add: { field: "product", item: JEANS, replace_index: -1 } },
    );
    const events = recordActions();

    fireEvent.click(
      screen.getByRole("button", { name: "Generate Photo Try-On" }),
    );
    const results = await screen.findByRole("heading", { name: "Results" });
    const cards = within(
      results.closest("section") as HTMLElement,
    ).getAllByRole("article");
    expect(cards).toHaveLength(1);
    expect(within(cards[0]).getByText("Best")).toBeTruthy();
    expect(within(cards[0]).getByText("Score 90")).toBeTruthy();
    expect(
      within(cards[0]).queryByRole("button", { name: /animate/i }),
    ).toBeNull();
    await waitFor(() =>
      expect(events.map((e) => Object.keys(e)[0])).toEqual([
        "job_started",
        "job_finished",
      ]),
    );
  });

  it("shows a spin (an animated WebP) as an image and an older MP4 spin as a video, each downloaded in its format", async () => {
    const result = (index: number, uri: string) => ({
      index,
      status: "ready",
      score: 90,
      uri,
      url: `/api/media?uri=${encodeURIComponent(uri)}`,
    });
    const results = [
      result(0, "gs://b/runs/a/outputs/video-0.webp"),
      result(1, "gs://b/runs/b/outputs/video-0.mp4"),
    ];
    const state = { capability: "spin", results: { spin: results } };
    server.use(
      http.get("/api/studio/:id/state", () =>
        HttpResponse.json({ studio_id: ID, saved: true, updated_at: 1, state }),
      ),
    );
    await renderStudio();

    const [spin, older] = screen.getAllByRole("article");
    expect(spin.querySelector("video")).toBeNull();
    expect(spin.querySelector("img")?.getAttribute("src")).toBe(results[0].url);
    expect(
      within(spin)
        .getByLabelText("Download Variation 1")
        .getAttribute("download"),
    ).toBe("variation-1.webp");
    expect(older.querySelector("video")?.getAttribute("src")).toBe(
      results[1].url,
    );
    expect(
      within(older)
        .getByLabelText("Download Variation 2")
        .getAttribute("download"),
    ).toBe("variation-2.mp4");
  });

  it("takes the Photo Try-On inputs on Video Try-On, with no image start, the reference model by upload only", async () => {
    await renderStudio({ capability: "video_tryon" });
    for (const title of [
      "Person",
      "Products",
      "Reference model (optional)",
      "Style",
      "Motion",
    ])
      expect(card(title)).toBeTruthy();
    expect(
      within(card("Reference model (optional)")).queryByText("or choose from"),
    ).toBeNull();
    expect(within(card("Person")).getByText("or choose from")).toBeTruthy();
    // "Animate an image" is gone: no start tabs, no image field.
    expect(screen.queryByRole("tablist", { name: "Video start" })).toBeNull();
    expect(screen.queryByRole("tab", { name: "Animate an image" })).toBeNull();
    expect(
      screen.queryByRole("heading", { name: "Image to animate" }),
    ).toBeNull();
    expect(screen.queryByLabelText("Upload Image to animate")).toBeNull();
    expect(screen.getByText("Choose a person first.")).toBeTruthy();
  });

  it("opens and closes the chat drawer", async () => {
    await renderStudio();
    const open = screen.getByRole("button", { name: "Open the studio agent" });

    fireEvent.click(open);
    expect(open.getAttribute("aria-expanded")).toBe("true");
    fireEvent.click(
      within(chat()).getByRole("button", { name: "Close the chat" }),
    );
    expect(open.getAttribute("aria-expanded")).toBe("false");
  });

  it("shows the reference model options once there is one and saves them as try-on settings", async () => {
    await renderStudio();
    const events = recordActions();
    const reference = () => card("Reference model (optional)");
    expect(
      screen.queryByRole("switch", { name: "Complete the look" }),
    ).toBeNull();

    // The user's own look photo: upload only, no gallery.
    expect(
      within(reference()).queryByRole("button", { name: "Gallery" }),
    ).toBeNull();
    fireEvent.change(
      screen.getByLabelText("Upload Reference model (optional)"),
      { target: { files: [imageFile("look.png")] } },
    );

    const look = await within(reference()).findByRole("switch", {
      name: "Complete the look",
    });
    const upscale = within(reference()).getByRole("switch", {
      name: "Upscale look-photo crops",
    });
    expect([
      look.getAttribute("aria-checked"),
      upscale.getAttribute("aria-checked"),
    ]).toEqual(["true", "false"]);
    expect(
      screen.getByText(
        /Sharpen the shoes taken from the reference model photo/,
      ),
    ).toBeTruthy();

    fireEvent.click(look);
    fireEvent.click(upscale);

    expect(look.getAttribute("aria-checked")).toBe("false");
    await waitFor(() =>
      expect(events.slice(1)).toEqual([
        { settings: { complete_look: false }, page: "tryon" },
        { settings: { upscale_crops: true }, page: "tryon" },
      ]),
    );
  });
});
