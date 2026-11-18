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
  AudioLines,
  AudioWaveform,
  Brain,
  ChevronLeft,
  CircleUserRound,
  Clock,
  Cloud,
  HardDrive,
} from "lucide-react";
import { type ReactNode, useState } from "react";
import {
  type OrchestratorConfig,
  useOrchestratorActions,
  useOrchestratorConfig,
  useOrchestratorState,
} from "../../../lib/api";
import { computerStatus, WORKSPACE_STATUS_TEXT } from "../../../lib/computer";
import { BrandIcon, HarnessBrandIcon } from "../../BrandIcons";
import { Section } from "../../common/primitives";
import { ErrorNote, LinkButton, Row, Switch } from "../../common/ui";
import { BottomSheet } from "../BottomSheet";
import { useDrillIn } from "../useDrillIn";
import { AppPage } from "./AppPage";
import { MemorySection } from "./MemorySection";
import { appStatus, voiceModeLabel } from "./options";
import {
  HarnessPicker,
  TimezonePicker,
  timezoneSummary,
  VoiceModePicker,
  VoicePicker,
} from "./PreferencePickers";

/** The pages of Settings: the root list, a picker, or one app's page. */
type SettingsPage =
  | { kind: "root" }
  | { kind: "harness" }
  | { kind: "voice" }
  | { kind: "voiceMode" }
  | { kind: "timezone" }
  | { kind: "memory" }
  | { kind: "app"; name: string };

const PAGE_TITLES: Record<Exclude<SettingsPage["kind"], "app">, string> = {
  root: "Settings",
  harness: "Coding agent",
  voice: "Voice",
  voiceMode: "Voice mode",
  timezone: "Timezone",
  memory: "Memory",
};

/** An app's page is titled by its name from `/config`, which may still be loading. */
function pageTitle(page: SettingsPage, config?: OrchestratorConfig): string {
  if (page.kind !== "app") return PAGE_TITLES[page.kind];
  return config?.apps.find((a) => a.name === page.name)?.display_name ?? "App";
}

/**
 * Settings: a full-screen page of plain rows. Every choice is a row that opens its
 * picker, and every app a row that opens its page, with one back button for all.
 * `app` opens it straight on that app's page (the voice's `navigate`).
 */
export function ConnectionsSheet({
  app,
  onClose,
}: {
  app?: string;
  onClose: () => void;
}) {
  const { data: config, isError } = useOrchestratorConfig();
  const nav = useDrillIn<SettingsPage>(
    { kind: "root" },
    app ? { kind: "app", name: app } : undefined,
  );

  const header = nav.parent ? (
    <button
      type="button"
      className="fl-back"
      onClick={nav.pop}
      aria-label={`Back to ${pageTitle(nav.parent, config)}`}
    >
      <ChevronLeft size={18} aria-hidden />
      {pageTitle(nav.parent, config)}
    </button>
  ) : (
    <span className="sheet-title">Settings</span>
  );

  return (
    <BottomSheet
      testId="configuration-sheet"
      header={header}
      closeLabel="Close settings"
      bodyClassName="connections-body"
      page
      onClose={onClose}
    >
      {config ? (
        <div
          ref={nav.viewRef}
          key={nav.depth}
          className="cfg-view"
          data-direction={nav.direction}
        >
          {nav.page.kind !== "root" && (
            <h2 className="cfg-detail-title">{pageTitle(nav.page, config)}</h2>
          )}
          <SettingsPageBody page={nav.page} config={config} onOpen={nav.push} />
        </div>
      ) : (
        <div className="connections-note">
          {isError ? "Couldn't load your settings." : "Loading…"}
        </div>
      )}
    </BottomSheet>
  );
}

function SettingsPageBody({
  page,
  config,
  onOpen,
}: {
  page: SettingsPage;
  config: OrchestratorConfig;
  onOpen: (page: SettingsPage) => void;
}) {
  const [error, setError] = useState<string | null>(null);
  const actions = useOrchestratorActions(setError);
  const prefs = config.preferences;
  const updatePreferences = (
    patch: Parameters<typeof actions.updatePreferences.mutate>[0],
  ) => {
    setError(null);
    actions.updatePreferences.mutate(patch);
  };

  let body: ReactNode;
  switch (page.kind) {
    case "root":
      body = (
        <SettingsRoot config={config} onOpen={onOpen} onError={setError} />
      );
      break;
    case "harness":
      body = (
        <HarnessPicker
          harnesses={config.harnesses}
          onChange={(name) => {
            setError(null);
            actions.setHarness.mutate(name);
          }}
        />
      );
      break;
    case "voice":
      body = <VoicePicker prefs={prefs} onUpdate={updatePreferences} />;
      break;
    case "voiceMode":
      body = <VoiceModePicker prefs={prefs} onUpdate={updatePreferences} />;
      break;
    case "timezone":
      body = <TimezonePicker prefs={prefs} onUpdate={updatePreferences} />;
      break;
    case "memory":
      body = <MemorySection />;
      break;
    case "app": {
      const app = config.apps.find((a) => a.name === page.name);
      body = app ? (
        <AppPage
          app={app}
          onDisconnect={() => {
            setError(null);
            actions.disconnect.mutate(app.name);
          }}
        />
      ) : (
        <div className="connections-note">This app isn't set up here.</div>
      );
      break;
    }
  }
  return (
    <>
      {error && <ErrorNote>{error}</ErrorNote>}
      {body}
    </>
  );
}

/** The root list: account, work, apps, voice and time, memory, and the workspace. */
function SettingsRoot({
  config,
  onOpen,
  onError,
}: {
  config: OrchestratorConfig;
  onOpen: (page: SettingsPage) => void;
  onError: (message: string | null) => void;
}) {
  const actions = useOrchestratorActions(onError);
  const state = useOrchestratorState().data;
  const workspace =
    computerStatus(state?.threads ?? [], state?.computer) ?? "stopped";
  const prefs = config.preferences;
  const harness = config.harnesses.find((h) => h.is_default);
  return (
    <>
      {config.account && (
        <RowGroup>
          <Row
            testId="account-header"
            icon={<CircleUserRound size={18} />}
            title={config.account.user_id}
            subtitle={
              config.account.sign_out_url
                ? "Signed in with Google"
                : "Local dev user"
            }
            trailing={
              config.account.sign_out_url && (
                <LinkButton
                  size="sm"
                  variant="quiet"
                  href={config.account.sign_out_url}
                >
                  Sign out
                </LinkButton>
              )
            }
          />
        </RowGroup>
      )}

      {harness && (
        <RowGroup label="Work">
          <Row
            icon={<HarnessBrandIcon icon={harness.icon} size={18} />}
            title="Coding agent"
            value={harness.display_name}
            // With one harness there is nothing to pick.
            onClick={
              config.harnesses.length > 1
                ? () => onOpen({ kind: "harness" })
                : undefined
            }
          />
        </RowGroup>
      )}

      {config.apps.length > 0 ? (
        <RowGroup label="Apps">
          {config.apps.map((app) => (
            <Row
              key={app.name}
              testId={`app-${app.name}`}
              icon={<BrandIcon icon={app.icon} size={18} />}
              title={app.display_name}
              value={appStatus(app)}
              onClick={() => onOpen({ kind: "app", name: app.name })}
            />
          ))}
        </RowGroup>
      ) : (
        // Apps are registered once per deployment; until then there's nothing to connect.
        <Section label="Apps">
          <p className="connections-note" data-testid="apps-empty">
            No apps are set up here yet. Run <code>make onboard</code> to add
            Google or GitHub.
          </p>
        </Section>
      )}

      <RowGroup label="Voice & time">
        <Row
          icon={<AudioLines size={17} />}
          title="Voice"
          value={prefs.voice_name}
          onClick={() => onOpen({ kind: "voice" })}
        />
        <Row
          testId="voice-mode"
          icon={<AudioWaveform size={17} />}
          title="Voice mode"
          subtitle="Takes effect next conversation"
          value={voiceModeLabel(prefs.voice_mode)}
          onClick={() => onOpen({ kind: "voiceMode" })}
        />
        <Row
          icon={<Clock size={17} />}
          title="Timezone"
          value={timezoneSummary(prefs.timezone)}
          onClick={() => onOpen({ kind: "timezone" })}
        />
        <Row
          icon={<HardDrive size={17} />}
          title="Save files to Drive"
          subtitle={
            prefs.save_to_drive === "on"
              ? "Finished files go to a folder per project. Sharing still asks first."
              : "Files stay on your computer."
          }
          wrap
          trailing={
            <Switch
              checked={prefs.save_to_drive === "on"}
              onChange={(on) =>
                actions.updatePreferences.mutate({
                  save_to_drive: on ? "on" : "off",
                })
              }
              label="Save files to Drive"
            />
          }
        />
      </RowGroup>

      {/* Always shown: without an Agent Engine it says so rather than disappearing. */}
      <RowGroup>
        <Row
          icon={<Brain size={17} />}
          title="Memory"
          value={config.memory_enabled ? "On" : "Not set up"}
          onClick={
            config.memory_enabled ? () => onOpen({ kind: "memory" }) : undefined
          }
        />
      </RowGroup>

      {/* For power users: whether the machine threads run on is awake. */}
      <RowGroup>
        <Row
          testId="workspace-status"
          icon={<Cloud size={17} />}
          title="Workspace"
          value={WORKSPACE_STATUS_TEXT[workspace]}
        />
      </RowGroup>
    </>
  );
}

/** Rows under an optional section label, as one plain list. */
function RowGroup({
  label,
  children,
}: {
  label?: string;
  children: ReactNode;
}) {
  const list = <div className="ui-list">{children}</div>;
  return label ? <Section label={label}>{list}</Section> : list;
}
