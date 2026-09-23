# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import getpass
import os
import sys
import urllib.parse

import google.auth
import requests
from google.auth.transport.requests import Request

_REQUEST_TIMEOUT = 30


def get_config(var_name, prompt, default=None, is_secret=False):
    """Helper to get config from env or user input."""
    value = os.environ.get(var_name)
    if not value:
        full_prompt = f"{prompt} [{default}]: " if default else f"{prompt}: "
        if is_secret:
            value = getpass.getpass(prompt + ": ")
        else:
            value = input(full_prompt).strip() or default
    return value


def register_oauth():
    print("--- [ Interactive A2A OAuth Registration ] ---")

    # 1. Gather configuration interactively or from env
    project_id = get_config(
        "GOOGLE_CLOUD_PROJECT", "Enter Google Cloud Project ID"
    )
    location = get_config(
        "LOCATION", "Enter Location (global/eu/us)", default="global"
    )
    # Multi-region endpoint mapping: global/us -> us, eu -> eu
    endpoint_location = os.environ.get(
        "ENDPOINT_LOCATION", "eu" if location == "eu" else "global"
    )

    auth_id = get_config(
        "AUTH_ID", "Enter Authorization ID (arbitrary ID for registration)"
    )
    client_id = get_config("OAUTH_CLIENT_ID", "Enter OAuth Client ID")
    client_secret = get_config(
        "OAUTH_CLIENT_SECRET", "Enter OAuth Client Secret", is_secret=True
    )

    token_uri = os.environ.get(
        "OAUTH_TOKEN_URI", "https://oauth2.googleapis.com/token"
    )

    # Default to drive.readonly scope for reading Google Drive files
    default_scopes = "https://www.googleapis.com/auth/drive.readonly"
    scopes = get_config(
        "OAUTH_SCOPES",
        "Enter OAuth Scopes (space-separated)",
        default=default_scopes,
    )

    # Final check on required fields (if user left them blank)
    if not all([project_id, auth_id, client_id, client_secret]):
        print("❌ Error: Missing required configuration details.")
        sys.exit(1)

    # 2. Get Google Auth Token
    try:
        credentials, _ = google.auth.default()
        if not credentials.valid:
            credentials.refresh(Request())
        access_token = credentials.token
    except Exception as e:
        print(f"❌ Error getting Google Auth token: {e}")
        sys.exit(1)

    # 3. Construct the Authorization URI
    base_auth_url = "https://accounts.google.com/o/oauth2/v2/auth"
    params = {
        "client_id": client_id,
        "redirect_uri": "https://vertexaisearch.cloud.google.com/oauth-redirect",
        "scope": scopes,
        "include_granted_scopes": "true",
        "response_type": "code",
        "access_type": "offline",
        "prompt": "consent",
    }
    auth_uri = f"{base_auth_url}?{urllib.parse.urlencode(params)}"

    # 4. Prepare the API request
    base_url = f"https://{endpoint_location}-discoveryengine.googleapis.com/v1alpha/projects/{project_id}/locations/{location}/authorizations"
    resource_name = (
        f"projects/{project_id}/locations/{location}/authorizations/{auth_id}"
    )

    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
        "X-Goog-User-Project": project_id,
    }

    payload = {
        "name": resource_name,
        "serverSideOauth2": {
            "clientId": client_id,
            "clientSecret": client_secret,
            "authorizationUri": auth_uri,
            "tokenUri": token_uri,
        },
    }

    # 5. Execute registration (with delete/recreate logic)
    print(f"\nRegistering Authorization resource '{auth_id}' in {location}...")

    try:
        # Step 5a: Attempt POST
        response = requests.post(
            f"{base_url}?authorizationId={auth_id}",
            headers=headers,
            json=payload,
            timeout=_REQUEST_TIMEOUT,
        )

        if response.status_code == 200:
            print("✅ Successfully registered authorization resource.")
            print(response.json())
        elif response.status_code == 409:
            # Step 5b: Handle Conflict by deleting and recreating
            print(
                f"⚠️  Authorization resource '{auth_id}' already exists. Overriding (Delete & Recreate)..."
            )

            del_response = requests.delete(
                f"{base_url}/{auth_id}",
                headers=headers,
                timeout=_REQUEST_TIMEOUT,
            )
            if del_response.status_code in [200, 204]:
                print("🗑️  Old resource deleted. Re-creating...")
                # Retry POST
                retry_response = requests.post(
                    f"{base_url}?authorizationId={auth_id}",
                    headers=headers,
                    json=payload,
                    timeout=_REQUEST_TIMEOUT,
                )
                if retry_response.status_code == 200:
                    print(
                        "✅ Successfully re-registered authorization resource."
                    )
                    print(retry_response.json())
                else:
                    print(
                        f"❌ Failed to re-create after delete. Status: {retry_response.status_code}"
                    )
                    print(retry_response.text)
            else:
                print(
                    f"❌ Failed to delete existing resource. Status: {del_response.status_code}"
                )
                print(del_response.text)
        else:
            print(f"❌ Failed to register. Status: {response.status_code}")
            print(response.text)

    except Exception as e:
        print(f"❌ Request failed: {e}")


if __name__ == "__main__":
    register_oauth()
