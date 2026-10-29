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

import { afterEach, describe, expect, it, vi } from "vitest";
import { MAX_UPLOAD_BYTES, shrinkImage, uploadFile } from "./shrink";

const photo = new File(["x"], "jacket.png", { type: "image/png" });

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

/** A file of `size` bytes. */
const sized = (name: string, type: string, size: number) =>
  new File([new Uint8Array(size)], name, { type });

/** A decodable 4000x3000 image whose shrunk copy is a JPEG. */
function stubCanvas() {
  vi.stubGlobal("createImageBitmap", () =>
    Promise.resolve({ width: 4000, height: 3000, close: vi.fn() }),
  );
  vi.spyOn(HTMLCanvasElement.prototype, "getContext").mockReturnValue({
    fillRect: vi.fn(),
    drawImage: vi.fn(),
  } as unknown as CanvasRenderingContext2D);
  vi.spyOn(HTMLCanvasElement.prototype, "toBlob").mockImplementation(
    (done, type) => done(new Blob(["jpeg"], { type })),
  );
}

describe("uploadFile", () => {
  it.each([
    ["photo.png", "image/png"],
    ["photo.jpg", "image/jpeg"],
    ["photo.webp", "image/webp"],
    ["IMG_1.HEIC", "image/heic"],
    ["IMG_1.heif", "image/heif"],
    ["shop.avif", "image/avif"],
    ["IMG_2.HEIC", ""],
  ])("sends %s (%s) as it is, up to the server's limit", async (name, type) => {
    stubCanvas();
    const original = sized(name, type, MAX_UPLOAD_BYTES);

    expect(await uploadFile(original)).toBe(original);
  });

  it("shrinks a photo over the server's limit", async () => {
    stubCanvas();

    const out = await uploadFile(
      sized("big.jpg", "image/jpeg", MAX_UPLOAD_BYTES + 1),
    );

    expect(out.name).toBe("big.jpg");
    expect(out.type).toBe("image/jpeg");
    expect(out.size).toBe(4);
  });

  it("shrinks a type the server does not take, as before", async () => {
    stubCanvas();

    const out = await uploadFile(sized("anim.gif", "image/gif", 10));

    expect(out.type).toBe("image/jpeg");
  });

  it("sends a large file the browser cannot decode as it is (the server says why not)", async () => {
    vi.stubGlobal("createImageBitmap", () =>
      Promise.reject(new Error("cannot decode")),
    );
    const heic = sized("IMG_3.HEIC", "image/heic", MAX_UPLOAD_BYTES + 1);

    expect(await uploadFile(heic)).toBe(heic);
  });
});

describe("shrinkImage", () => {
  it("keeps a file the browser cannot decode (HEIC on most browsers)", async () => {
    vi.stubGlobal("createImageBitmap", () =>
      Promise.reject(new Error("cannot decode")),
    );
    const heic = new File(["x"], "IMG_1.HEIC", { type: "image/heic" });

    expect(await shrinkImage(heic)).toBe(heic);
  });

  it("makes a JPEG of at most 1600 px a side with the same name", async () => {
    const close = vi.fn();
    vi.stubGlobal("createImageBitmap", () =>
      Promise.resolve({ width: 4000, height: 3000, close }),
    );
    const draw = vi.fn();
    const canvases: HTMLCanvasElement[] = [];
    vi.spyOn(HTMLCanvasElement.prototype, "getContext").mockImplementation(
      function (this: HTMLCanvasElement) {
        canvases.push(this);
        return {
          fillRect: vi.fn(),
          drawImage: draw,
        } as unknown as CanvasRenderingContext2D;
      } as never,
    );
    vi.spyOn(HTMLCanvasElement.prototype, "toBlob").mockImplementation(
      (done, type) => done(new Blob(["jpeg"], { type })),
    );

    const out = await shrinkImage(photo);

    expect([canvases[0].width, canvases[0].height]).toEqual([1600, 1200]);
    expect(draw).toHaveBeenCalledWith(expect.anything(), 0, 0, 1600, 1200);
    expect(close).toHaveBeenCalled();
    expect(out.name).toBe("jacket.png");
    expect(out.type).toBe("image/jpeg");
  });
});
