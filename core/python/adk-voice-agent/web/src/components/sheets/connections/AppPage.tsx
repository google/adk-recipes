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

import { ExternalLink } from "lucide-react";
import type { AppInfo } from "../../../lib/api";
import { BrandIcon } from "../../BrandIcons";
import { useAppConnect } from "../../common/AppConnect";
import { Button, ErrorNote, LinkButton, Row } from "../../common/ui";

/**
 * One app's page: who it's connected as and what it covers, then its actions.
 * Not connected: Connect. Connected: Grant access when it needs more, Choose
 * repos when the app has somewhere to choose them, and Disconnect.
 */
export function AppPage({
  app,
  onDisconnect,
}: {
  app: AppInfo;
  onDisconnect: () => void;
}) {
  const { connect, busy, error } = useAppConnect();
  return (
    <div className="app-page" data-testid={`app-page-${app.name}`}>
      <div className="ui-list">
        <Row
          icon={<BrandIcon icon={app.icon} size={20} />}
          title={
            app.connected ? app.account_label || "Connected" : "Not connected"
          }
          subtitle={app.services.join(", ") || undefined}
          wrap
        />
      </div>
      {app.connected && app.needs_grant && (
        <p className="app-page-note">
          {app.display_name} needs more access than you gave it when you
          connected.
        </p>
      )}
      <div className="app-page-actions">
        {!app.connected && (
          <Button
            variant="primary"
            size="sm"
            disabled={busy}
            onClick={() => void connect(app.name)}
          >
            Connect
          </Button>
        )}
        {app.connected && app.needs_grant && (
          <Button
            variant="primary"
            size="sm"
            disabled={busy}
            onClick={() => void connect(app.name)}
          >
            Grant access
          </Button>
        )}
        {app.connected && app.manage_url && (
          <LinkButton
            size="sm"
            href={app.manage_url}
            target="_blank"
            rel="noreferrer"
            icon={<ExternalLink size={14} aria-hidden />}
          >
            Choose repos
          </LinkButton>
        )}
        {app.connected && (
          <Button size="sm" onClick={onDisconnect}>
            Disconnect
          </Button>
        )}
      </div>
      {error && <ErrorNote>{error}</ErrorNote>}
    </div>
  );
}
