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

import { CloseIcon } from "./icons";
import { Dialog } from "./Modal";

interface ImageLightboxProps {
  src: string;
  alt?: string;
  onClose: () => void;
}

export default function ImageLightbox({
  src,
  alt = "Preview",
  onClose,
}: ImageLightboxProps) {
  return (
    <Dialog label={alt} onClose={onClose} className="relative">
      <img
        src={src}
        alt={alt}
        className="block max-w-[calc(100vw-2rem)] max-h-[calc(100dvh-2rem)] object-contain rounded-lg shadow-2xl"
      />
      <button
        type="button"
        onClick={onClose}
        aria-label="Close"
        className="absolute top-2 right-2 w-10 h-10 bg-black rounded-full flex items-center justify-center shadow-lg hover:scale-110 transition-transform"
      >
        <CloseIcon size={20} />
      </button>
    </Dialog>
  );
}
