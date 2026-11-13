# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""The page a sign-in popup ends on: it reports the result to the web app and closes."""

from __future__ import annotations

import html
import json
from typing import Any

from fastapi.responses import HTMLResponse

_TOKENS = """
    :root {
      --bg-outer: #07080a;
      --text-primary: #f4f6fb;
      --icon-muted: #94a3b8;
      --accent-emerald: #10b981;
      --accent-coral-hover: #ef4444;
    }"""


def _script_json(value: Any) -> str:
    return (
        json.dumps(value)
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("&", "\\u0026")
    )


def render_oauth_popup_html(
    *,
    provider: str,
    ok: bool,
    title: str,
    subtitle: str,
    opener_origin: str | None,
    account_label: str | None = None,
) -> HTMLResponse:
    status_color = (
        "var(--accent-emerald)" if ok else "var(--accent-coral-hover)"
    )
    message = _script_json(
        {
            "type": "adk_oauth_complete",
            "provider": provider,
            "ok": ok,
            "account_label": account_label,
        }
    )
    notify = (
        f"if (window.opener) window.opener.postMessage({message}, {_script_json(opener_origin)});"
        if opener_origin
        else ""
    )
    title, subtitle = html.escape(title), html.escape(subtitle)
    page = f"""<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8" />
  <title>{title}</title>
  <style>{_TOKENS}
    body {{
      margin: 0;
      height: 100vh;
      display: flex;
      align-items: center;
      justify-content: center;
      background: var(--bg-outer);
      color: var(--text-primary);
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    }}
    .card {{
      max-width: 360px;
      padding: 28px 24px;
      border-radius: 18px;
      background: rgba(255,255,255,0.04);
      border: 1px solid rgba(255,255,255,0.1);
      text-align: center;
    }}
    .badge {{
      display: inline-block;
      width: 12px;
      height: 12px;
      border-radius: 50%;
      background: {status_color};
      margin-bottom: 12px;
    }}
    h2 {{ margin: 0 0 8px; font-size: 18px; }}
    p {{ margin: 0; font-size: 13.5px; color: var(--icon-muted); line-height: 1.5; }}
  </style>
</head>
<body>
  <div class="card">
    <div class="badge"></div>
    <h2>{title}</h2>
    <p>{subtitle}</p>
  </div>
  <script>
    try {{
      {notify}
    }} catch (e) {{}}
    setTimeout(function() {{ window.close(); }}, 900);
  </script>
</body>
</html>"""
    return HTMLResponse(content=page)
