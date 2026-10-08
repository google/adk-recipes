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

import { useMemo, useState } from "react";
import type { TrajectoryDoc } from "@/lib/trajectory/types";
import { Ledger } from "./ledger";
import { Detail } from "./detail";

export function Trajectory({ doc }: { doc: TrajectoryDoc }) {
  const [selectedId, setSelectedId] = useState<string | undefined>(undefined);

  const selected = useMemo(
    () => doc.records.find((r) => r.id === selectedId),
    [doc.records, selectedId],
  );

  const onSelect = (id: string) => {
    setSelectedId((prev) => (prev === id ? undefined : id));
  };

  return (
    <div className="flex flex-col rounded-lg border bg-card/50 overflow-hidden">
      <Ledger
        records={doc.records}
        selectedId={selectedId}
        onSelect={onSelect}
      />
      {selected && (
        <Detail record={selected} onClose={() => setSelectedId(undefined)} />
      )}
    </div>
  );
}
