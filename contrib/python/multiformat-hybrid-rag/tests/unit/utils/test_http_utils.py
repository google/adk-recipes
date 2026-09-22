from unittest.mock import MagicMock, patch

import pytest
import requests

from src.utils.http_utils import post_with_retry


@patch("src.utils.http_utils.random.random", return_value=0.5)
@patch("src.utils.http_utils.time.sleep")
@patch("src.utils.http_utils.requests.post")
class TestPostWithRetry:
    def test_success_on_first_try(self, mock_post, mock_sleep, mock_random):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"result": "ok"}
        mock_resp.raise_for_status.return_value = None
        mock_post.return_value = mock_resp

        result = post_with_retry("http://example.com", {}, {}, timeout=10)

        assert result == {"result": "ok"}
        mock_sleep.assert_not_called()

    def test_retry_on_500_then_success(
        self, mock_post, mock_sleep, mock_random
    ):
        error_resp = MagicMock()
        error_resp.status_code = 500
        http_err = requests.HTTPError(response=error_resp)
        error_resp.raise_for_status.side_effect = http_err

        ok_resp = MagicMock()
        ok_resp.json.return_value = {"ok": True}
        ok_resp.raise_for_status.return_value = None

        mock_post.side_effect = [error_resp, ok_resp]

        result = post_with_retry("http://x.com", {}, {}, timeout=5, attempts=3)
        assert result == {"ok": True}
        assert mock_sleep.call_count == 1

    def test_retry_on_connection_error(
        self, mock_post, mock_sleep, mock_random
    ):
        conn_err = requests.ConnectionError("connection refused")

        ok_resp = MagicMock()
        ok_resp.json.return_value = {"recovered": True}
        ok_resp.raise_for_status.return_value = None

        mock_post.side_effect = [conn_err, ok_resp]

        result = post_with_retry("http://x.com", {}, {}, timeout=5, attempts=3)
        assert result == {"recovered": True}
        assert mock_sleep.call_count == 1

    def test_non_retryable_400_raises_immediately(
        self, mock_post, mock_sleep, mock_random
    ):
        error_resp = MagicMock()
        error_resp.status_code = 400
        http_err = requests.HTTPError(response=error_resp)
        error_resp.raise_for_status.side_effect = http_err
        mock_post.return_value = error_resp

        with pytest.raises(requests.HTTPError):
            post_with_retry("http://x.com", {}, {}, timeout=5)
        mock_sleep.assert_not_called()

    def test_429_retries_without_counting_attempts(
        self, mock_post, mock_sleep, mock_random
    ):
        error_resp = MagicMock()
        error_resp.status_code = 429
        http_err = requests.HTTPError(response=error_resp)
        error_resp.raise_for_status.side_effect = http_err

        ok_resp = MagicMock()
        ok_resp.json.return_value = {"ok": True}
        ok_resp.raise_for_status.return_value = None

        mock_post.side_effect = [error_resp, error_resp, ok_resp]

        result = post_with_retry("http://x.com", {}, {}, timeout=5, attempts=1)
        assert result == {"ok": True}
        assert mock_sleep.call_count == 2

    def test_exhausted_retries_raises_last_exception(
        self, mock_post, mock_sleep, mock_random
    ):
        error_resp = MagicMock()
        error_resp.status_code = 503
        http_err = requests.HTTPError(
            "service unavailable", response=error_resp
        )
        error_resp.raise_for_status.side_effect = http_err
        mock_post.return_value = error_resp

        with pytest.raises(requests.HTTPError, match="service unavailable"):
            post_with_retry("http://x.com", {}, {}, timeout=5, attempts=2)
        assert mock_post.call_count == 2
