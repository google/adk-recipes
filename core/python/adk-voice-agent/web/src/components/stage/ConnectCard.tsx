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

import type { AppInfo } from "../../lib/api";
import { connectLine } from "../../lib/starters";
import { BrandIcon } from "../BrandIcons";
import { useAppConnect } from "../common/AppConnect";
import { Button, ErrorNote } from "../common/ui";

/** Home's ask to connect an app (first run: Google), with the same sign-in as Settings. */
export function ConnectCard({ app }: { app: AppInfo }) {
  const { connect, busy, error } = useAppConnect();
  return (
    <article className="a2ui-card connect-card" data-testid="connect-card">
      <header className="card-head">
        <span className="card-source">
          <BrandIcon icon={app.icon} size={18} />
          <span className="card-source-label">{app.display_name}</span>
        </span>
      </header>
      <p className="connect-card-text">{connectLine(app)}</p>
      <Button
        variant="primary"
        size="sm"
        disabled={busy}
        onClick={() => void connect(app.name)}
      >
        Connect
      </Button>
      {error && <ErrorNote>{error}</ErrorNote>}
    </article>
  );
}
