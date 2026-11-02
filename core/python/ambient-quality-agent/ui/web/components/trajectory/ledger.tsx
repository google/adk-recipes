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

import type { TrajectoryRecord } from "@/lib/trajectory/types";
import { textStyle } from "@/lib/typography";
import { cn } from "@/lib/utils";
import { Row } from "./row";

interface LedgerProps {
  records: TrajectoryRecord[];
  selectedId: string | undefined;
  onSelect: (id: string) => void;
}

export function Ledger({ records, selectedId, onSelect }: LedgerProps) {
  if (records.length === 0) {
    return <p className={cn(textStyle.meta, "p-4")}>No steps recorded.</p>;
  }

  return (
    <div className="flex flex-col gap-0.5 p-1.5 overflow-y-auto">
      {records.map((record) => (
        <Row
          key={record.id}
          record={record}
          selected={record.id === selectedId}
          onSelect={onSelect}
        />
      ))}
    </div>
  );
}
