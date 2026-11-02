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
  type ReactNode,
  useContext,
  useEffect,
  useRef,
  useState,
} from "react";
import { plainErrorMessage, startOAuth, useRefreshConfig } from "../../lib/api";

const POPUP_FEATURES =
  "width=520,height=660,menubar=no,toolbar=no,location=no,status=no";

type OAuthComplete = {
  type: "adk_oauth_complete";
  provider: string;
  ok: boolean;
};

function isOAuthComplete(data: unknown): data is OAuthComplete {
  const msg = data as Partial<OAuthComplete> | null;
  return (
    msg?.type === "adk_oauth_complete" &&
    typeof msg.provider === "string" &&
    typeof msg.ok === "boolean"
  );
}

/** Origin of the callback page the popup ends on: the backend's, which differs under Vite. */
function callbackOrigin(authorizeUrl: string): string | null {
  try {
    const redirect = new URL(authorizeUrl).searchParams.get("redirect_uri");
    return redirect ? new URL(redirect).origin : null;
  } catch {
    return null;
  }
}

export interface AppConnect {
  connect: (name: string) => Promise<void>;
  busy: boolean;
  error: string | null;
}

const AppConnectContext = createContext<AppConnect | null>(null);

export function AppConnectProvider({
  onConnected,
  children,
}: {
  onConnected: (name: string) => void;
  children: ReactNode;
}) {
  const refresh = useRefreshConfig();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const popup = useRef<{ window: Window | null; origin: string | null } | null>(
    null,
  );
  const connected = useRef(onConnected);
  connected.current = onConnected;

  useEffect(() => {
    const handler = (event: MessageEvent) => {
      const opened = popup.current;
      if (!opened || event.source !== opened.window) return;
      if (
        event.origin !== window.location.origin &&
        event.origin !== opened.origin
      )
        return;
      if (!isOAuthComplete(event.data)) return;
      popup.current = null;
      void refresh();
      if (event.data.ok) connected.current(event.data.provider);
    };
    window.addEventListener("message", handler);
    return () => window.removeEventListener("message", handler);
  }, [refresh]);

  const connect = async (name: string) => {
    setBusy(true);
    setError(null);
    const win = window.open("about:blank", `adk_oauth_${name}`, POPUP_FEATURES);
    if (!win) {
      setBusy(false);
      setError("Allow pop-ups in your browser to connect.");
      return;
    }
    try {
      const { authorize_url, callback_origin } = await startOAuth(name);
      win.location.href = authorize_url;
      popup.current = {
        window: win,
        origin: callback_origin ?? callbackOrigin(authorize_url),
      };
    } catch (err) {
      win.close();
      setError(plainErrorMessage(err, "Couldn't start sign-in. Try again."));
    } finally {
      setBusy(false);
    }
  };

  return (
    <AppConnectContext.Provider value={{ connect, busy, error }}>
      {children}
    </AppConnectContext.Provider>
  );
}

/** The shared connect flow; must be inside `AppConnectProvider`. */
export function useAppConnect(): AppConnect {
  const value = useContext(AppConnectContext);
  if (!value) throw new Error("useAppConnect needs an AppConnectProvider");
  return value;
}
