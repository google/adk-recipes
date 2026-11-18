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

import { File, FileText, Folder, Presentation, Sheet } from "lucide-react";
import {
  editedLabel,
  FILE_KIND_LABELS,
  type FileKind,
  fileKind,
  safeHref,
} from "../../../lib/answerCards";
import type { FileItem } from "../../../lib/cards";
import { keyedByText } from "../../../lib/keys";
import { Row, Rows } from "../context/parts";

const ICONS: Record<FileKind, typeof File> = {
  doc: FileText,
  sheet: Sheet,
  slides: Presentation,
  pdf: FileText,
  folder: Folder,
  file: File,
};

/** Drive files: a type tile in the file's Google color, then the name and when it was edited. */
export function FilesCard({ items }: { items: FileItem[] }) {
  return (
    <Rows>
      {keyedByText(items, (f) => f.id).map(({ key, item: file }) => {
        const kind = fileKind(file.mime_type);
        const Icon = ICONS[kind];
        return (
          <Row key={key} href={safeHref(file.web_view_link)}>
            <span
              className="drive-tile"
              data-kind={kind}
              role="img"
              aria-label={FILE_KIND_LABELS[kind]}
            >
              <Icon size={16} />
            </span>
            <div className="drive-main">
              <p className="drive-name">{file.name || "(Untitled)"}</p>
              <p className="drive-meta">
                {editedLabel(file.modified_time) || FILE_KIND_LABELS[kind]}
              </p>
            </div>
          </Row>
        );
      })}
    </Rows>
  );
}
