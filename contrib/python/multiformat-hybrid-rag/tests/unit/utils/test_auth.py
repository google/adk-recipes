from unittest.mock import MagicMock, patch

import pytest

import src.utils.auth as auth_module
from src.utils.auth import _mint_id_token, get_id_token


@pytest.fixture(autouse=True)
def reset_module_state():
    auth_module._token_cache.clear()
    auth_module._credentials = None
    auth_module._auth_request = None
    yield
    auth_module._token_cache.clear()
    auth_module._credentials = None
    auth_module._auth_request = None


class TestGetIdToken:
    @patch("src.utils.auth.time.sleep")
    @patch("src.utils.auth._mint_id_token")
    @patch("src.utils.auth.time.monotonic")
    def test_returns_cached_token_if_fresh(
        self, mock_monotonic, mock_mint, mock_sleep
    ):
        mock_monotonic.return_value = 100.0
        auth_module._token_cache["https://svc.run.app"] = ("cached-token", 90.0)

        result = get_id_token("https://svc.run.app")

        assert result == "cached-token"
        mock_mint.assert_not_called()

    @patch("src.utils.auth.time.sleep")
    @patch("src.utils.auth._mint_id_token", return_value="new-token")
    @patch("src.utils.auth.time.monotonic")
    def test_refreshes_expired_token(
        self, mock_monotonic, mock_mint, mock_sleep
    ):
        mock_monotonic.return_value = 5000.0
        auth_module._token_cache["https://svc.run.app"] = ("old-token", 0.0)

        result = get_id_token("https://svc.run.app")

        assert result == "new-token"
        mock_mint.assert_called_once_with("https://svc.run.app")

    @patch("src.utils.auth.time.sleep")
    @patch("src.utils.auth._mint_id_token")
    @patch("src.utils.auth.time.monotonic")
    def test_retry_on_mint_failure(self, mock_monotonic, mock_mint, mock_sleep):
        mock_monotonic.return_value = 5000.0
        mock_mint.side_effect = [
            RuntimeError("fail1"),
            RuntimeError("fail2"),
            "refreshed-token",
        ]

        result = get_id_token("https://svc.run.app")

        assert result == "refreshed-token"
        assert mock_mint.call_count == 3
        assert mock_sleep.call_count == 2

    @patch("src.utils.auth.time.sleep")
    @patch("src.utils.auth._mint_id_token")
    @patch("src.utils.auth.time.monotonic", return_value=5000.0)
    def test_raises_after_all_retries_exhausted(
        self, mock_monotonic, mock_mint, mock_sleep
    ):
        mock_mint.side_effect = RuntimeError("persistent failure")

        with pytest.raises(RuntimeError, match="persistent failure"):
            get_id_token("https://svc.run.app")

        assert mock_mint.call_count == 5

    @patch("src.utils.auth.time.sleep")
    @patch("src.utils.auth._mint_id_token", return_value="fresh-token")
    @patch("src.utils.auth.time.monotonic", return_value=100.0)
    def test_mints_new_token_when_cache_empty(
        self, mock_monotonic, mock_mint, mock_sleep
    ):
        result = get_id_token("https://svc.run.app")

        assert result == "fresh-token"
        mock_mint.assert_called_once_with("https://svc.run.app")


class TestMintIdToken:
    @patch("src.utils.auth._get_credentials")
    @patch("google.oauth2.id_token.fetch_id_token", return_value="meta-token")
    def test_metadata_path(self, mock_fetch, mock_get_creds):
        mock_creds = MagicMock()
        mock_auth_req = MagicMock()
        mock_get_creds.return_value = (mock_creds, mock_auth_req)

        result = _mint_id_token("https://svc.run.app")
        assert result == "meta-token"

    @patch("src.utils.auth._get_credentials")
    def test_impersonated_credentials_fallback(self, mock_get_creds):
        mock_creds = MagicMock()
        mock_creds.service_account_email = "sa@project.iam.gserviceaccount.com"
        mock_auth_req = MagicMock()
        mock_get_creds.return_value = (mock_creds, mock_auth_req)

        with patch(
            "google.oauth2.id_token.fetch_id_token",
            side_effect=Exception("no metadata"),
        ):
            mock_id_token_creds = MagicMock()
            mock_id_token_creds.token = "impersonated-token"
            with patch(
                "google.auth.impersonated_credentials.IDTokenCredentials",
                return_value=mock_id_token_creds,
            ):
                result = _mint_id_token("https://svc.run.app")

        assert result == "impersonated-token"
        mock_id_token_creds.refresh.assert_called_once_with(mock_auth_req)

    @patch("src.utils.auth._get_credentials")
    def test_access_token_fallback(self, mock_get_creds):
        mock_creds = MagicMock(spec=[])
        mock_creds.token = "access-token"
        mock_creds.refresh = MagicMock()
        mock_auth_req = MagicMock()
        mock_get_creds.return_value = (mock_creds, mock_auth_req)

        with patch(
            "google.oauth2.id_token.fetch_id_token",
            side_effect=Exception("no metadata"),
        ):
            result = _mint_id_token("https://svc.run.app")

        assert result == "access-token"
        mock_creds.refresh.assert_called_once_with(mock_auth_req)
