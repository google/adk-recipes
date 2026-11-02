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

import { useEffect, useState } from "react";
import { X } from "lucide-react";
import type { TrajectoryRecord } from "@/lib/trajectory/types";
import { defaultTabFor, tabsFor } from "@/lib/trajectory/tabs";
import { PreviewTab } from "./tabs/preview-tab";
import { ToolsTab } from "./tabs/tools-tab";
import { RawTab } from "./tabs/raw-tab";
import { Button } from "@/components/ui/button";
import { textStyle } from "@/lib/typography";
import { cn, formatSentenceCase } from "@/lib/utils";

export function Detail({
  record,
  onClose,
}: {
  record: TrajectoryRecord;
  onClose: () => void;
}) {
  const tabs = tabsFor(record);
  const [tabId, setTabId] = useState<string>(defaultTabFor(record));

  useEffect(() => {
    setTabId(defaultTabFor(record));
  }, [record]);

  const currentTabId = tabs.some((t) => t.id === tabId)
    ? tabId
    : defaultTabFor(record);

  return (
    <div className="flex flex-col border-t bg-card text-card-foreground">
      <div className="flex items-center justify-between border-b px-3 py-1.5 bg-muted/20">
        <div className="flex items-center gap-2">
          <span className={textStyle.meta}>#{record.index}</span>
          <span className={cn(textStyle.label, "text-foreground")}>
            {formatSentenceCase(record.kind)}
          </span>
        </div>
        <Button
          size="sm"
          variant="ghost"
          onClick={onClose}
          className="h-6 w-6 p-0 text-muted-foreground hover:text-foreground"
          aria-label="Close detail"
        >
          <X className="h-3.5 w-3.5" />
        </Button>
      </div>

      <div
        role="tablist"
        aria-label="Trajectory detail tabs"
        className="flex border-b bg-muted/10 px-2 gap-1"
      >
        {tabs.map((t) => (
          <button
            key={t.id}
            role="tab"
            aria-selected={t.id === currentTabId}
            type="button"
            className={cn(
              textStyle.label,
              "px-2.5 py-1 transition-colors border-b-2 -mb-px",
              t.id === currentTabId
                ? "border-primary text-foreground"
                : "border-transparent text-muted-foreground hover:text-foreground",
            )}
            onClick={() => setTabId(t.id)}
          >
            {t.label}
          </button>
        ))}
      </div>

      <div className="max-h-72 overflow-y-auto">
        {currentTabId === "preview" && <PreviewTab record={record} />}
        {currentTabId === "tools" && <ToolsTab record={record} />}
        {currentTabId === "raw" && <RawTab record={record} />}
      </div>
    </div>
  );
}
