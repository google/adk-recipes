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

"""`RestClient`: every failure discovery can meet arrives as an `ApiError`.

httpx is faked with a `MockTransport` behind `httpx.request`, the one call the
client makes.
"""

from __future__ import annotations

import httpx
import pytest
from ambient_quality_cli import rest_client
from ambient_quality_cli.rest_client import ApiError, RestClient
from ambient_quality_shared import usage_headers
from ambient_quality_shared.usage_headers import build_product_token


def _serve(monkeypatch, handler) -> list[httpx.Request]:
    """Routes the client's requests to ``handler``.

    Args:
        monkeypatch: Pytest monkeypatch fixture.
        handler: Takes an `httpx.Request`, returns an `httpx.Response`.

    Returns:
        The requests sent, in order.
    """
    sent: list[httpx.Request] = []

    def record(request: httpx.Request) -> httpx.Response:
        """Records a request and answers it.

        Args:
            request: The request sent.

        Returns:
            The handler's response.
        """
        sent.append(request)
        return handler(request)

    transport = httpx.MockTransport(record)

    def fake_request(method, url, **kwargs):
        """Stands in for `httpx.request`, over the mock transport.

        Args:
            method: HTTP method.
            url: Request URL.
            **kwargs: The client's other arguments; the timeout is dropped.

        Returns:
            The response.
        """
        kwargs.pop("timeout", None)
        with httpx.Client(transport=transport) as client:
            return client.request(method, url, **kwargs)

    monkeypatch.setattr(rest_client.httpx, "request", fake_request)
    return sent


def _build_client() -> RestClient:
    """Builds a client with a fixed token.

    Returns:
        The client.
    """
    return RestClient(lambda: "tok")


def test_sends_the_token_and_decodes_the_reply(monkeypatch):
    sent = _serve(monkeypatch, lambda r: httpx.Response(200, json={"a": 1}))

    assert _build_client().get_json("https://x/y", params={"p": "1"}) == {
        "a": 1
    }
    assert sent[0].headers["Authorization"] == "Bearer tok"
    assert sent[0].url.params["p"] == "1"


def test_an_empty_reply_is_an_empty_object(monkeypatch):
    _serve(monkeypatch, lambda r: httpx.Response(204))

    assert _build_client().post_json("https://x/y", {"q": 1}) == {}


def test_an_error_status_carries_the_apis_message(monkeypatch):
    _serve(
        monkeypatch,
        lambda r: httpx.Response(
            403, json={"error": {"message": "Permission denied"}}
        ),
    )

    with pytest.raises(ApiError) as caught:
        _build_client().get_json("https://x/y")
    assert (caught.value.status, caught.value.message) == (
        403,
        "Permission denied",
    )


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(302, headers={"Location": "https://elsewhere"}),
        httpx.Response(200, text="<html>sign in</html>"),
        httpx.Response(200, json=["not", "an", "object"]),
    ],
)
def test_a_reply_that_is_not_a_json_object_is_an_api_error(
    monkeypatch, response
):
    _serve(monkeypatch, lambda r: response)

    with pytest.raises(ApiError):
        _build_client().get_json("https://x/y")


def test_a_transport_failure_is_an_api_error(monkeypatch):
    def fail(request):
        """Fails the request as a lost connection would.

        Args:
            request: The request sent.

        Raises:
            httpx.ConnectError: Always.
        """
        raise httpx.ConnectError("no route", request=request)

    _serve(monkeypatch, fail)

    with pytest.raises(ApiError) as caught:
        _build_client().get_json("https://x/y")
    assert caught.value.status == 0


def test_an_error_carries_its_error_info_reason(monkeypatch):
    _serve(
        monkeypatch,
        lambda r: httpx.Response(
            403,
            json={
                "error": {
                    "message": "Observability API has not been used",
                    "details": [
                        {"@type": "type.googleapis.com/google.rpc.Help"},
                        {
                            "@type": "type.googleapis.com/google.rpc.ErrorInfo",
                            "reason": "SERVICE_DISABLED",
                        },
                    ],
                }
            },
        ),
    )

    with pytest.raises(ApiError) as caught:
        _build_client().get_json("https://x/y")
    assert caught.value.reason == "SERVICE_DISABLED"


@pytest.mark.parametrize(
    "body",
    [
        {"error": {"message": "denied"}},
        {"error": {"message": "denied", "details": "not a list"}},
        {"error": "not an object"},
    ],
)
def test_an_error_without_error_info_has_no_reason(monkeypatch, body):
    _serve(monkeypatch, lambda r: httpx.Response(403, json=body))

    with pytest.raises(ApiError) as caught:
        _build_client().get_json("https://x/y")
    assert caught.value.reason == ""


def test_sends_the_product_token(monkeypatch, usage_analytics_on):
    sent = _serve(monkeypatch, lambda r: httpx.Response(200, json={}))

    RestClient(lambda: "tok").get_json("https://x/y")

    assert sent[0].headers["user-agent"] == build_product_token()
    assert sent[0].headers["x-goog-api-client"] == build_product_token()
    assert sent[0].headers["Authorization"] == "Bearer tok"


def test_sends_httpx_defaults_with_the_switch_off(monkeypatch):
    monkeypatch.setattr(usage_headers, "USAGE_ANALYTICS_ENABLED", False)
    sent = _serve(monkeypatch, lambda r: httpx.Response(200, json={}))

    RestClient(lambda: "tok").get_json("https://x/y")

    assert sent[0].headers["user-agent"] == f"python-httpx/{httpx.__version__}"
    assert "x-goog-api-client" not in sent[0].headers
