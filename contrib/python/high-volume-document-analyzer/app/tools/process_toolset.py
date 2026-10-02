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

# app/tools/process_toolset.py

import asyncio
import base64
import logging
import os
import ssl
import time
from typing import Any

import aiohttp
import google.auth
from dotenv import load_dotenv
from google.cloud import secretmanager

load_dotenv()

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s"
)

URL_TOKEN_API_URL = os.getenv("URL_TOKEN_API_URL")
DOCUMENT_API_BASE_URL = os.getenv("DOCUMENT_API_BASE_URL") or ""
USE_MOCK_API = str(os.getenv("USE_MOCK_API")).lower() == "true"
MAX_CONCURRENT_DOWNLOADS = int(os.environ["MAX_CONCURRENT_DOWNLOADS"])
ALLOWED_MIME_TYPES = {
    "application/pdf",
    "text/html",
    "text/plain",
    "image/png",
    "image/jpeg",
    "image/webp",
}

GENERIC_MIME_TYPES = {
    "application/octet-stream",
    "binary/octet-stream",
    "application/force-download",
}

_SECRET_CLIENT = None
_CACHED_CREDENTIALS: dict[str, str | None] = {"key": None, "secret": None}
_TOKEN_CACHE: dict[str, Any] = {"access_token": None, "expires_at": 0.0}

_SSL_CONTEXT = ssl.create_default_context()


def get_secret_client():
    """Singleton for Secret Manager Client to avoid gRPC overhead."""
    global _SECRET_CLIENT
    if _SECRET_CLIENT is None:
        _SECRET_CLIENT = secretmanager.SecretManagerServiceClient()
    return _SECRET_CLIENT


def get_secret(
    project_id: str, secret_id: str, version_id: str = "latest"
) -> str | None:
    """Fetches secret from Secret Manager efficiently."""
    try:
        client = get_secret_client()
        name = client.secret_version_path(
            project=project_id, secret=secret_id, secret_version=version_id
        )
        response = client.access_secret_version(request={"name": name})
        return response.payload.data.decode("UTF-8")
    except Exception as e:
        logging.warning(f"SECRET_MANAGER: Error fetching '{secret_id}': {e}")
        return None


def get_credentials() -> tuple[str | None, str | None]:
    """Retrieves credentials using local cache or environment variables."""
    if _CACHED_CREDENTIALS["key"] and _CACHED_CREDENTIALS["secret"]:
        return _CACHED_CREDENTIALS["key"], _CACHED_CREDENTIALS["secret"]

    # 1. Try local environment variables first (useful for local testing)
    key = os.getenv("CLIENT_ID")
    secret = os.getenv("CLIENT_SECRET")

    if (
        key and secret and not key.startswith("prod-")
    ):  # Basic check to see if it's a real key or just a name
        _CACHED_CREDENTIALS["key"] = key
        _CACHED_CREDENTIALS["secret"] = secret
        return key, secret

    # 2. Fallback to Secret Manager
    try:
        _creds, project_id = google.auth.default()
        if not project_id:
            project_id = os.getenv("GOOGLE_CLOUD_PROJECT")

        if not project_id:
            logging.info(
                "Project ID not auto-detected, ensuring env vars or secrets exist."
            )
            return None, None

        # Here key/secret are the NAMES of the secrets in Secret Manager
        # We fetch their actual content
        key_content = get_secret(project_id, key or "CLIENT_ID")
        secret_content = get_secret(project_id, secret or "CLIENT_SECRET")

        if key_content and secret_content:
            _CACHED_CREDENTIALS["key"] = key_content
            _CACHED_CREDENTIALS["secret"] = secret_content
            return key_content, secret_content

    except Exception as e:
        logging.error(
            f"AUTH CONFIG: Error loading credentials from Secret Manager: {e}"
        )

    return None, None


async def get_auth_token_async() -> str:
    """
    Obtains access token asynchronously.
    Avoids repeated calls to the authentication server.
    """
    current_time = time.time()

    if _TOKEN_CACHE["access_token"] and current_time < (
        _TOKEN_CACHE["expires_at"] - 60
    ):
        return _TOKEN_CACHE["access_token"]

    consumer_key, consumer_secret = get_credentials()
    if not consumer_key or not consumer_secret:
        raise ValueError("Credentials not found.")

    basic = f"{consumer_key}:{consumer_secret}"
    base64_basic = base64.b64encode(basic.encode("ascii")).decode("ascii")
    headers = {
        "Authorization": f"Basic {base64_basic}",
        "Content-Type": "application/x-www-form-urlencoded",
    }
    data = {"grant_type": "client_credentials"}

    logging.info("AUTH: Renewing access token...")

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                URL_TOKEN_API_URL,
                data=data,
                headers=headers,
                ssl=_SSL_CONTEXT,
                timeout=10,
            ) as resp:
                resp.raise_for_status()
                response_json = await resp.json()

                token = response_json["access_token"]
                expires_in = int(response_json.get("expires_in", 3600))

                _TOKEN_CACHE["access_token"] = token
                _TOKEN_CACHE["expires_at"] = current_time + expires_in

                return token

    except Exception as e:
        logging.error(f"AUTH: Critical failure obtaining token: {e}")
        raise


async def fetch_document_urls_async(collection_id: str) -> list[str]:
    """Fetches list of URLs from the API."""

    # ==========================================
    # [REMOVE FOR PRODUCTION]
    # Mock logic for local testing without Authentication
    if USE_MOCK_API:
        logging.info(
            f"MOCK API: Fetching mock document list for collection '{collection_id}'"
        )
        return [
            "https://www.w3.org/WAI/ER/tests/xhtml/testfiles/resources/pdf/dummy.pdf",
        ]
    # ==========================================

    # --- PRODUCTION LOGIC BEGINS HERE ---
    try:
        token = await get_auth_token_async()
        headers = {
            "accept": "application/json",
            "Authorization": f"Bearer {token}",
        }
        if "{collection_id}" in DOCUMENT_API_BASE_URL:
            url = DOCUMENT_API_BASE_URL.format(collection_id=collection_id)
        else:
            url = f"{DOCUMENT_API_BASE_URL}/{collection_id}"

        logging.info(f"API: Fetching list at {url}")

        async with aiohttp.ClientSession() as session:
            async with session.get(
                url, headers=headers, ssl=_SSL_CONTEXT, timeout=30
            ) as resp:
                if resp.status != 200:
                    logging.error(
                        f"API: List error {resp.status} - {await resp.text()}"
                    )
                    return []

                data = await resp.json()

                if isinstance(data, list):
                    logging.info(f"API: {len(data)} docs found.")
                    return data
                return []

    except Exception as e:
        logging.error(f"API: Error fetching urls: {e}")
        return []


async def download_file_async(
    session: aiohttp.ClientSession, url: str, semaphore: asyncio.Semaphore
) -> dict | None:
    """
    Verifies the header BEFORE downloading the file.
    Saves bandwidth by ignoring not allowed types.
    """
    async with semaphore:
        try:
            async with session.get(url, ssl=_SSL_CONTEXT, timeout=60) as resp:
                if resp.status != 200:
                    return None

                raw_content_type = (
                    resp.headers.get("Content-Type", "")
                    .split(";")[0]
                    .strip()
                    .lower()
                )

                if (
                    raw_content_type not in ALLOWED_MIME_TYPES
                    and raw_content_type not in GENERIC_MIME_TYPES
                ):
                    logging.info(
                        f"SKIPPING: Header indicates '{raw_content_type}' (Not allowed) at {url}"
                    )
                    return None

                content = await resp.read()

                final_mime_type = raw_content_type

                if content.startswith(b"%PDF"):
                    final_mime_type = "application/pdf"

                if final_mime_type not in ALLOWED_MIME_TYPES:
                    logging.info(
                        f"DISCARDED: Downloaded file was '{final_mime_type}' (Invalid)."
                    )
                    return None

                if final_mime_type == "text/html":
                    return {
                        "mime_type": "text/html",
                        "data": content.decode("utf-8", errors="ignore"),
                        "is_binary": False,
                    }
                elif final_mime_type in [
                    "application/pdf",
                    "image/png",
                    "image/jpeg",
                    "image/webp",
                ]:
                    return {
                        "mime_type": final_mime_type,
                        "data": content,
                        "is_binary": True,
                    }
                else:
                    return {
                        "mime_type": "text/plain",
                        "data": content.decode("utf-8", errors="ignore"),
                        "is_binary": False,
                    }

        except Exception as e:
            logging.error(f"DOWNLOAD: Failed at {url}: {e!s}")
            return None


async def download_batch_async(urls: list[str]) -> list[dict | None]:
    """
    Downloads multiple files in parallel with concurrency control.
    """
    semaphore = asyncio.Semaphore(MAX_CONCURRENT_DOWNLOADS)

    async with aiohttp.ClientSession() as session:
        tasks = [download_file_async(session, url, semaphore) for url in urls]
        return await asyncio.gather(*tasks)
