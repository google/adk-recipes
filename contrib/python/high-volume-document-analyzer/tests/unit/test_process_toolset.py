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

"""Unit tests for the process API toolset logic."""

import asyncio
import socket
import ssl
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.tools.process_toolset import (
    _SSL_CONTEXT,
    _validate_download_url,
    download_file_async,
    fetch_document_urls_async,
)


def _fake_addrinfo(
    ip: str, port: int = 443, family: int = socket.AF_INET
) -> list[tuple[Any, ...]]:
    sockaddr: tuple[Any, ...] = (
        (ip, port, 0, 0) if family == socket.AF_INET6 else (ip, port)
    )
    return [(family, socket.SOCK_STREAM, 6, "", sockaddr)]


@pytest.mark.asyncio
async def test_fetch_mock_urls() -> None:
    """Verify that when USE_MOCK_API is True, the mock URL list is predictably returned."""
    with patch("app.tools.process_toolset.USE_MOCK_API", True):
        urls = await fetch_document_urls_async("test_collection_123")
        assert len(urls) == 1
        assert "dummy.pdf" in urls[0]
        assert "w3.org" in urls[0]


@pytest.mark.asyncio
async def test_fetch_real_urls_empty_on_error() -> None:
    """Verify that an exception inside the real API call safely returns an empty list."""
    with (
        patch("app.tools.process_toolset.USE_MOCK_API", False),
        patch(
            "app.tools.process_toolset.get_auth_token_async",
            side_effect=Exception("Mock Auth Failure"),
        ),
    ):
        urls = await fetch_document_urls_async("test_collection")
        assert urls == []


def test_ssl_context_enforces_verification() -> None:
    """Verify TLS hostname and certificate verification remain enabled."""
    assert _SSL_CONTEXT.check_hostname is True
    assert _SSL_CONTEXT.verify_mode == ssl.CERT_REQUIRED


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bad_url",
    [
        "file:///etc/passwd",
        "ftp://example.com/doc.pdf",
        "gopher://127.0.0.1:80/",
        "https:///missing-host.pdf",
    ],
)
async def test_validate_download_url_rejects_bad_schemes_and_empty_host(
    bad_url: str,
) -> None:
    """Verify non-HTTP(S) schemes and hostless URLs are rejected."""
    with pytest.raises(ValueError):
        await _validate_download_url(bad_url)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("url", "resolved_ip", "family"),
    [
        ("http://127.0.0.1/secret", "127.0.0.1", socket.AF_INET),
        ("http://localhost/secret", "127.0.0.1", socket.AF_INET),
        (
            "http://169.254.169.254/computeMetadata/v1/",
            "169.254.169.254",
            socket.AF_INET,
        ),
        ("http://10.0.0.5/internal", "10.0.0.5", socket.AF_INET),
        ("http://172.16.0.1/internal", "172.16.0.1", socket.AF_INET),
        ("http://192.168.1.1/internal", "192.168.1.1", socket.AF_INET),
        ("http://0.0.0.0/internal", "0.0.0.0", socket.AF_INET),
        ("http://[::1]/secret", "::1", socket.AF_INET6),
        ("http://[fe80::1]/secret", "fe80::1", socket.AF_INET6),
        (
            "http://[::ffff:169.254.169.254]/secret",
            "::ffff:169.254.169.254",
            socket.AF_INET6,
        ),
    ],
)
async def test_validate_download_url_blocks_non_public_ips(
    url: str, resolved_ip: str, family: int
) -> None:
    """Verify URLs resolving to loopback, private, or link-local IPs are rejected."""
    loop = asyncio.get_running_loop()
    with patch.object(
        loop,
        "getaddrinfo",
        new=AsyncMock(return_value=_fake_addrinfo(resolved_ip, family=family)),
    ):
        with pytest.raises(ValueError, match="blocked non-public address"):
            await _validate_download_url(url)


@pytest.mark.asyncio
async def test_validate_download_url_blocks_mixed_dns() -> None:
    """Verify multi-record DNS responses are rejected if any address is private."""
    loop = asyncio.get_running_loop()
    mixed = _fake_addrinfo("93.184.216.34") + _fake_addrinfo("10.0.0.1")
    with patch.object(loop, "getaddrinfo", new=AsyncMock(return_value=mixed)):
        with pytest.raises(ValueError, match="blocked non-public address"):
            await _validate_download_url("https://docs.example.com/file.pdf")


@pytest.mark.asyncio
async def test_validate_download_url_enforces_host_allowlist() -> None:
    """Verify ALLOWED_DOCUMENT_HOSTS restricts downloads to permitted hosts."""
    loop = asyncio.get_running_loop()
    with (
        patch(
            "app.tools.process_toolset.ALLOWED_DOCUMENT_HOSTS",
            frozenset({"docs.example.com"}),
        ),
        patch.object(
            loop,
            "getaddrinfo",
            new=AsyncMock(return_value=_fake_addrinfo("93.184.216.34")),
        ),
    ):
        await _validate_download_url("https://docs.example.com/file.pdf")
        with pytest.raises(ValueError, match="ALLOWED_DOCUMENT_HOSTS"):
            await _validate_download_url("https://other.example.com/file.pdf")


@pytest.mark.asyncio
async def test_download_file_async_blocks_ssrf_before_http_request() -> None:
    """Verify download_file_async returns None without calling session.get on internal URLs."""
    session = MagicMock()
    semaphore = asyncio.Semaphore(1)
    loop = asyncio.get_running_loop()

    with patch.object(
        loop,
        "getaddrinfo",
        new=AsyncMock(return_value=_fake_addrinfo("169.254.169.254")),
    ):
        result = await download_file_async(
            session, "http://169.254.169.254/computeMetadata/v1/", semaphore
        )

    assert result is None
    session.get.assert_not_called()


@pytest.mark.asyncio
async def test_download_file_async_disables_redirects_for_public_url() -> None:
    """Verify valid public downloads pass allow_redirects=False and verified SSL context."""
    mock_resp = MagicMock()
    mock_resp.status = 200
    mock_resp.headers = {"Content-Type": "text/plain; charset=utf-8"}
    mock_resp.read = AsyncMock(return_value=b"hello document")

    mock_ctx = MagicMock()
    mock_ctx.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_ctx.__aexit__ = AsyncMock(return_value=None)

    session = MagicMock()
    session.get.return_value = mock_ctx
    semaphore = asyncio.Semaphore(1)
    loop = asyncio.get_running_loop()

    with patch.object(
        loop,
        "getaddrinfo",
        new=AsyncMock(return_value=_fake_addrinfo("93.184.216.34")),
    ):
        result = await download_file_async(
            session, "https://docs.example.com/doc.txt", semaphore
        )

    assert result == {
        "mime_type": "text/plain",
        "data": "hello document",
        "is_binary": False,
    }
    session.get.assert_called_once_with(
        "https://docs.example.com/doc.txt",
        ssl=_SSL_CONTEXT,
        timeout=60,
        allow_redirects=False,
    )


@pytest.mark.asyncio
async def test_download_file_async_returns_none_on_redirect() -> None:
    """Verify HTTP 3xx redirects are not followed and return None."""
    mock_resp = MagicMock()
    mock_resp.status = 302
    mock_resp.headers = {
        "Location": "http://169.254.169.254/computeMetadata/v1/"
    }

    mock_ctx = MagicMock()
    mock_ctx.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_ctx.__aexit__ = AsyncMock(return_value=None)

    session = MagicMock()
    session.get.return_value = mock_ctx
    semaphore = asyncio.Semaphore(1)
    loop = asyncio.get_running_loop()

    with patch.object(
        loop,
        "getaddrinfo",
        new=AsyncMock(return_value=_fake_addrinfo("93.184.216.34")),
    ):
        result = await download_file_async(
            session, "https://docs.example.com/redirect", semaphore
        )

    assert result is None


@pytest.mark.asyncio
async def test_fetch_real_urls_encodes_collection_id() -> None:
    """Verify collection_id is URL-encoded before being interpolated into the API URL."""
    mock_resp = MagicMock()
    mock_resp.status = 200
    mock_resp.json = AsyncMock(return_value=["https://docs.example.com/a.pdf"])

    mock_get_ctx = MagicMock()
    mock_get_ctx.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_get_ctx.__aexit__ = AsyncMock(return_value=None)

    mock_session = MagicMock()
    mock_session.get.return_value = mock_get_ctx

    mock_session_ctx = MagicMock()
    mock_session_ctx.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session_ctx.__aexit__ = AsyncMock(return_value=None)

    with (
        patch("app.tools.process_toolset.USE_MOCK_API", False),
        patch(
            "app.tools.process_toolset.DOCUMENT_API_BASE_URL",
            "https://api.example.com/v1/documents/{collection_id}/list",
        ),
        patch(
            "app.tools.process_toolset.get_auth_token_async",
            new=AsyncMock(return_value="tok"),
        ),
        patch(
            "app.tools.process_toolset.aiohttp.ClientSession",
            return_value=mock_session_ctx,
        ),
    ):
        urls = await fetch_document_urls_async("../admin?x=1#frag")

    assert urls == ["https://docs.example.com/a.pdf"]
    called_url = mock_session.get.call_args.args[0]
    assert called_url == (
        "https://api.example.com/v1/documents/..%2Fadmin%3Fx%3D1%23frag/list"
    )
