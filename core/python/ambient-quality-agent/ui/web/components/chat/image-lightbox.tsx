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

import {
  createContext,
  useCallback,
  useContext,
  useState,
  type ReactNode,
} from "react";
import * as DialogPrimitive from "@radix-ui/react-dialog";
import { X } from "lucide-react";

type OpenImage = (src: string, alt?: string) => void;

// No-op fallback so an image outside the provider never throws.
const ImageLightboxContext = createContext<OpenImage>(() => {});

export function useImageLightbox(): OpenImage {
  return useContext(ImageLightboxContext);
}

export function ImageLightboxProvider({ children }: { children: ReactNode }) {
  const [image, setImage] = useState<{ src: string; alt: string } | null>(null);
  const open = useCallback<OpenImage>((src, alt) => {
    setImage({ src, alt: alt ?? "" });
  }, []);

  return (
    <ImageLightboxContext.Provider value={open}>
      {children}
      <DialogPrimitive.Root
        open={!!image}
        onOpenChange={(o) => {
          if (!o) setImage(null);
        }}
      >
        <DialogPrimitive.Portal>
          <DialogPrimitive.Overlay className="fixed inset-0 z-[60] bg-black/80 backdrop-blur-sm data-[state=open]:animate-in data-[state=closed]:animate-out data-[state=open]:fade-in-0 data-[state=closed]:fade-out-0" />
          <DialogPrimitive.Content className="fixed inset-0 z-[60] flex items-center justify-center p-4 focus:outline-none data-[state=open]:animate-in data-[state=closed]:animate-out data-[state=open]:fade-in-0 data-[state=closed]:fade-out-0">
            <DialogPrimitive.Title className="sr-only">
              Image preview
            </DialogPrimitive.Title>
            <DialogPrimitive.Description className="sr-only">
              Full-size image. Press Escape or click outside to close.
            </DialogPrimitive.Description>
            {image && (
              <img
                src={image.src}
                alt={image.alt}
                className="max-h-[90vh] max-w-[90vw] rounded-lg object-contain shadow-2xl"
              />
            )}
            <DialogPrimitive.Close
              aria-label="Close image"
              className="absolute right-4 top-4 rounded-full bg-black/50 p-2 text-white/90 hover:bg-black/70 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-white"
            >
              <X className="h-5 w-5" />
            </DialogPrimitive.Close>
          </DialogPrimitive.Content>
        </DialogPrimitive.Portal>
      </DialogPrimitive.Root>
    </ImageLightboxContext.Provider>
  );
}
