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

import time
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import MagicMock

import pytest
import requests
from google.api_core.exceptions import NotFound

from genmedia4commerce.core import gcs
from genmedia4commerce.core.errors import ConfigError, InputError

# ---------------------------------------------------------------------- gcs


@pytest.fixture
def storage(monkeypatch):
    client = MagicMock()
    ctor = MagicMock(return_value=client)
    gcs._client.cache_clear()
    monkeypatch.setattr(gcs.storage, "Client", ctor)
    yield ctor, client
    gcs._client.cache_clear()


def test_download(storage):
    ctor, client = storage
    client.bucket.return_value.blob.return_value.download_as_bytes.return_value = b"D"
    assert gcs.download("gs://bkt/a/b.png") == b"D"
    ctor.assert_called_once_with(project="test-project")
    client.bucket.assert_called_once_with("bkt")
    client.bucket.return_value.blob.assert_called_once_with("a/b.png")


def test_one_client_for_every_call(storage):
    ctor, _ = storage
    gcs.download("gs://bkt/a.png")
    gcs.upload(b"x", bucket="bkt", path="b.png", content_type="image/png")
    gcs.blob("gs://bkt/c.png")
    ctor.assert_called_once_with(project="test-project")


@pytest.mark.parametrize(
    "uri", ["https://x/y", "gs://bucket-only", "gs:///path", "x"]
)
def test_download_rejects_non_gs(storage, uri):
    with pytest.raises(InputError):
        gcs.download(uri)


def test_upload(storage):
    _, client = storage
    uri = gcs.upload(
        b"data", bucket="bkt", path="p/x.png", content_type="image/png"
    )
    assert uri == "gs://bkt/p/x.png"
    blob = client.bucket.return_value.blob.return_value
    blob.upload_from_string.assert_called_once_with(
        b"data", content_type="image/png"
    )


def test_resolve_image_bytes_passthrough(monkeypatch):
    monkeypatch.setattr(gcs, "download", MagicMock())
    assert gcs.resolve_image(b"raw") == b"raw"
    gcs.download.assert_not_called()


MEDIA_BUCKET = "test-project-genmedia-for-commerce-media-payloads"


def test_resolve_image_gs_from_the_media_bucket(monkeypatch):
    uri = f"gs://{MEDIA_BUCKET}/runs/2026-01-02/s/0123456789ab/inputs/a.png"
    monkeypatch.setattr(gcs, "download", MagicMock(return_value=b"G"))
    assert gcs.resolve_image(uri) == b"G"
    gcs.download.assert_called_once_with(uri)


def test_resolve_image_catalog_refs(monkeypatch, catalog_file):
    from genmedia4commerce.core import catalog

    monkeypatch.setattr(gcs, "download", MagicMock())
    assert gcs.resolve_image("catalog:pa1") == catalog.image("pa1")
    assert gcs.resolve_image("catalog:s_shoe/2") == catalog.image("s_shoe", 2)
    for bad in ("catalog:nope", "catalog:s_shoe/3", "catalog:pa1/x"):
        with pytest.raises(InputError):
            gcs.resolve_image(bad)
    gcs.download.assert_not_called()


def test_resolve_image_gs_follows_the_media_bucket_setting(monkeypatch):
    from genmedia4commerce.core.settings import get_settings

    monkeypatch.setenv("MEDIA_BUCKET", "my-media")
    get_settings.cache_clear()
    monkeypatch.setattr(gcs, "download", MagicMock(return_value=b"G"))
    assert gcs.resolve_image("gs://my-media/runs/x.png") == b"G"
    with pytest.raises(InputError, match="bucket"):
        gcs.resolve_image(f"gs://{MEDIA_BUCKET}/runs/x.png")


@pytest.mark.parametrize(
    "uri",
    [
        "gs://someone-elses-bucket/x.png",
        f"gs://{MEDIA_BUCKET}-evil/x.png",
        f"gs://{MEDIA_BUCKET}.evil/x.png",
        "gs:///x.png",
        # The media bucket: only runs/, so chat history and uploads under
        # sessions/ never reach a run folder (where /api/media serves them).
        f"gs://{MEDIA_BUCKET}/sessions/abc/history.json",
        f"gs://{MEDIA_BUCKET}/sessions/abc/user_uploads/a.png",
        f"gs://{MEDIA_BUCKET}/x.png",
        f"gs://{MEDIA_BUCKET}/runsx/a.png",
        f"gs://{MEDIA_BUCKET}/runs/../sessions/abc/history.json",
        f"gs://{MEDIA_BUCKET}/runs/2026-01-02/s/../../x.png",
    ],
)
def test_resolve_image_gs_rejects_other_buckets_and_paths(monkeypatch, uri):
    download = MagicMock()
    monkeypatch.setattr(gcs, "download", download)
    with pytest.raises(InputError):
        gcs.resolve_image(uri)
    download.assert_not_called()


def test_resolve_image_missing_gs_object_is_input_error(monkeypatch):
    def missing(uri):
        raise NotFound(uri)

    monkeypatch.setattr(gcs, "download", missing)
    with pytest.raises(InputError, match="not found"):
        gcs.resolve_image(f"gs://{MEDIA_BUCKET}/runs/gone.png")


class FakeResponse:
    """A streamed ``requests`` response."""

    def __init__(self, body: bytes = b"H", status: int = 200):
        self.body, self.status_code, self.read = body, status, 0

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def iter_content(self, chunk_size):
        for i in range(0, len(self.body), chunk_size):
            self.read = i + chunk_size
            yield self.body[i : i + chunk_size]


def fake_get(monkeypatch, response=None, error=None):
    def get(url, **kwargs):
        get.calls.append((url, kwargs))
        if error:
            raise error
        return response or FakeResponse()

    get.calls = []
    monkeypatch.setattr(gcs.requests, "get", get)
    return get


def test_resolve_image_https_allowlisted(monkeypatch):
    get = fake_get(monkeypatch, FakeResponse(b"H"))
    url = "https://storage.googleapis.com/b/x.png"
    assert gcs.resolve_image(url) == b"H"
    assert get.calls == [
        (url, {"timeout": 20, "stream": True, "allow_redirects": False})
    ]


@pytest.mark.parametrize("status", [404, 500, 301, 302])
def test_resolve_image_https_error_or_redirect_is_input_error(
    monkeypatch, status
):
    fake_get(monkeypatch, FakeResponse(b"<html>", status=status))
    with pytest.raises(InputError, match=f"HTTP {status}"):
        gcs.resolve_image("https://storage.googleapis.com/b/missing.png")


@pytest.mark.parametrize(
    "error",
    [requests.ConnectTimeout("slow"), requests.ConnectionError("refused")],
)
def test_resolve_image_https_fetch_failure_is_input_error(monkeypatch, error):
    fake_get(monkeypatch, error=error)
    with pytest.raises(InputError, match="could not fetch image"):
        gcs.resolve_image("https://storage.googleapis.com/b/x.png")


def test_resolve_image_https_stops_above_the_size_limit(monkeypatch):
    monkeypatch.setattr(gcs, "MAX_IMAGE_BYTES", 2**20)
    response = FakeResponse(b"x" * 5 * 2**20)
    fake_get(monkeypatch, response)
    with pytest.raises(InputError, match="larger than"):
        gcs.resolve_image("https://storage.googleapis.com/b/huge.png")
    assert response.read == 2 * 2**20  # aborted after the second chunk


def test_resolve_image_custom_allowlist(monkeypatch):
    from genmedia4commerce.core.settings import get_settings

    monkeypatch.setenv("GENMEDIA_ALLOWED_IMAGE_HOSTS", "cdn.example.com")
    get_settings.cache_clear()
    fake_get(monkeypatch, FakeResponse(b"C"))
    assert gcs.resolve_image("https://cdn.example.com/x.png") == b"C"
    with pytest.raises(InputError):
        gcs.resolve_image("https://storage.googleapis.com/b/x.png")


@pytest.mark.parametrize(
    "ref",
    [
        "https://evil.example.com/x.png",
        "https://169.254.169.254/latest/meta-data",
        "http://storage.googleapis.com/b/x.png",
        "ftp://storage.googleapis.com/b/x.png",
        "file:///etc/passwd",
        "/etc/passwd",
        12,
    ],
)
def test_resolve_image_rejects(monkeypatch, ref):
    get = MagicMock()
    monkeypatch.setattr(gcs.requests, "get", get)
    with pytest.raises(InputError):
        gcs.resolve_image(ref)
    get.assert_not_called()


# ------------------------------------------------------------------ signing


class FakeCredentials:
    """Service account credentials whose token can be expired at will."""

    def __init__(self, email: str | None = "sa@p.iam.gserviceaccount.com"):
        self.service_account_email = email
        self.token: str | None = None
        self.valid, self.refreshes = False, 0

    def refresh(self, _request) -> None:
        time.sleep(0.01)  # long enough for concurrent calls to overlap
        self.refreshes += 1
        self.token, self.valid = f"token-{self.refreshes}", True


@pytest.fixture
def credentials(monkeypatch, storage):
    _, client = storage
    signer = client.bucket.return_value.blob.return_value.generate_signed_url
    signer.return_value = "https://signed"
    creds = FakeCredentials()
    default = MagicMock(return_value=(creds, "p"))
    monkeypatch.setattr(gcs.google.auth, "default", default)
    gcs._credentials.cache_clear()
    yield creds, default, signer
    gcs._credentials.cache_clear()


def test_signing_loads_credentials_once_and_refreshes_when_expired(
    credentials,
):
    creds, default, signer = credentials
    assert gcs.signed_url("gs://bkt/a.png") == "https://signed"
    gcs.signed_url("gs://bkt/b.png")
    assert (default.call_count, creds.refreshes) == (1, 1)
    assert signer.call_args.kwargs["access_token"] == "token-1"
    assert signer.call_args.kwargs["service_account_email"] == (
        "sa@p.iam.gserviceaccount.com"
    )
    creds.valid = False  # the token expired
    gcs.signed_url("gs://bkt/c.png")
    assert (default.call_count, creds.refreshes) == (1, 2)
    assert signer.call_args.kwargs["access_token"] == "token-2"


def test_signing_refreshes_once_for_concurrent_calls(credentials):
    creds, _, _ = credentials
    with ThreadPoolExecutor(8) as pool:
        list(pool.map(gcs.signed_url, [f"gs://bkt/{i}.png" for i in range(16)]))
    assert creds.refreshes == 1


def test_credentials_that_cannot_sign_are_a_config_error(credentials):
    creds, _, signer = credentials
    creds.service_account_email = None  # e.g. user credentials
    with pytest.raises(ConfigError, match="cannot sign"):
        gcs.signed_url("gs://bkt/a.png")
    signer.assert_not_called()
