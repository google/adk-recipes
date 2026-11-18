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
  FileIcon,
  FileTextIcon,
  ImageIcon,
  type LucideIcon,
  MusicIcon,
  VideoIcon,
} from "lucide-react";

/**
 * Selects the icon for a MIME type so attachments render consistently across
 * chat and stored conversations.
 *
 * @param mimeType - MIME type of the file, or null if unknown.
 * @returns Lucide icon for the MIME family, or a generic file icon.
 */
export function resolveFileIcon(mimeType: string | null): LucideIcon {
  if (!mimeType) return FileIcon;
  if (mimeType.startsWith("image/")) return ImageIcon;
  if (mimeType.startsWith("audio/")) return MusicIcon;
  if (mimeType.startsWith("video/")) return VideoIcon;
  if (mimeType === "application/pdf" || mimeType.startsWith("text/")) {
    return FileTextIcon;
  }
  return FileIcon;
}
