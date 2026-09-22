import hashlib
import json
from unittest.mock import MagicMock, patch


class TestComputeFileId:
    def test_deterministic(self):
        from src.document_preprocessing.preprocess import compute_file_id

        uri = "gs://bucket/path/file.pdf"
        assert compute_file_id(uri) == compute_file_id(uri)

    def test_matches_md5_hex(self):
        from src.document_preprocessing.preprocess import compute_file_id

        uri = "gs://bucket/path/file.pdf"
        expected = hashlib.md5(uri.encode()).hexdigest()
        assert compute_file_id(uri) == expected

    def test_different_uris_give_different_ids(self):
        from src.document_preprocessing.preprocess import compute_file_id

        id1 = compute_file_id("gs://bucket/a.pdf")
        id2 = compute_file_id("gs://bucket/b.pdf")
        assert id1 != id2


class TestFindChangedWithDedup:
    def _make_row(
        self,
        file_id="f1",
        gcs_uri="gs://b/f.pdf",
        md5_hash="abc",
        file_name="f.pdf",
        cross_run_dup_file_id=None,
        rn=1,
        in_batch_first_file_id="f1",
    ):
        row = MagicMock()
        row.__getitem__ = lambda _, k: {
            "file_id": file_id,
            "gcs_uri": gcs_uri,
            "md5_hash": md5_hash,
            "file_name": file_name,
            "cross_run_dup_file_id": cross_run_dup_file_id,
            "rn": rn,
            "in_batch_first_file_id": in_batch_first_file_id,
        }[k]
        return row

    def test_returns_to_extract_for_non_dup_rows(self):
        from src.document_preprocessing.preprocess import (
            find_changed_with_dedup,
        )

        bq = MagicMock()
        query_job = MagicMock()
        query_job.result.return_value = [
            self._make_row(file_id="f1", rn=1, cross_run_dup_file_id=None),
        ]
        bq.query.return_value = query_job

        to_extract, dup_stubs = find_changed_with_dedup(
            bq, "proj.ds.obj", "proj.ds.prep", "documents/"
        )
        assert len(to_extract) == 1
        assert to_extract[0]["file_id"] == "f1"
        assert len(dup_stubs) == 0

    def test_returns_cross_run_dup_stub(self):
        from src.document_preprocessing.preprocess import (
            find_changed_with_dedup,
        )

        bq = MagicMock()
        query_job = MagicMock()
        query_job.result.return_value = [
            self._make_row(
                file_id="f2",
                cross_run_dup_file_id="f_original",
                rn=1,
            ),
        ]
        bq.query.return_value = query_job

        to_extract, dup_stubs = find_changed_with_dedup(
            bq, "proj.ds.obj", "proj.ds.prep", "documents/"
        )
        assert len(to_extract) == 0
        assert len(dup_stubs) == 1
        assert dup_stubs[0]["dup_of"] == "f_original"

    def test_returns_in_batch_dup_stub(self):
        from src.document_preprocessing.preprocess import (
            find_changed_with_dedup,
        )

        bq = MagicMock()
        query_job = MagicMock()
        query_job.result.return_value = [
            self._make_row(
                file_id="f3",
                cross_run_dup_file_id=None,
                rn=2,
                in_batch_first_file_id="f_first",
            ),
        ]
        bq.query.return_value = query_job

        to_extract, dup_stubs = find_changed_with_dedup(
            bq, "proj.ds.obj", "proj.ds.prep", "documents/"
        )
        assert len(to_extract) == 0
        assert len(dup_stubs) == 1
        assert dup_stubs[0]["dup_of"] == "f_first"

    def test_mixed_rows(self):
        from src.document_preprocessing.preprocess import (
            find_changed_with_dedup,
        )

        bq = MagicMock()
        query_job = MagicMock()
        query_job.result.return_value = [
            self._make_row(
                file_id="extract_me", rn=1, cross_run_dup_file_id=None
            ),
            self._make_row(
                file_id="cross_dup",
                rn=1,
                cross_run_dup_file_id="old_id",
            ),
            self._make_row(
                file_id="batch_dup",
                rn=2,
                cross_run_dup_file_id=None,
                in_batch_first_file_id="extract_me",
            ),
        ]
        bq.query.return_value = query_job

        to_extract, dup_stubs = find_changed_with_dedup(
            bq, "proj.ds.obj", "proj.ds.prep", "documents/"
        )
        assert len(to_extract) == 1
        assert len(dup_stubs) == 2


class TestResolveServiceUrl:
    def test_returns_env_var_override(self):
        from src.document_preprocessing.preprocess import _resolve_service_url

        with patch.dict(
            "os.environ", {"PREPROCESS_SERVICE_URL": "http://localhost:8080"}
        ):
            url = _resolve_service_url("my-project", "us-central1")
        assert url == "http://localhost:8080"

    def test_calls_cloud_run_api_when_no_override(self):
        from src.document_preprocessing.preprocess import _resolve_service_url

        mock_client = MagicMock()
        mock_service = MagicMock()
        mock_service.uri = "https://svc-abc123-uc.a.run.app"
        mock_client.return_value.get_service.return_value = mock_service

        with (
            patch.dict("os.environ", {}, clear=False),
            patch(
                "os.environ.get",
                side_effect=lambda k, *a: (
                    None
                    if k == "PREPROCESS_SERVICE_URL"
                    else (a[0] if a else None)
                ),
            ),
            patch(
                "src.document_preprocessing.preprocess.run_v2.ServicesClient",
                mock_client,
            ),
        ):
            import os

            prev = os.environ.pop("PREPROCESS_SERVICE_URL", None)
            try:
                url = _resolve_service_url("my-project", "us-central1")
            finally:
                if prev is not None:
                    os.environ["PREPROCESS_SERVICE_URL"] = prev

        assert url == "https://svc-abc123-uc.a.run.app"
        mock_client.return_value.get_service.assert_called_once()
        call_name = mock_client.return_value.get_service.call_args
        assert "my-project" in str(call_name)


class TestProcessOne:
    def test_sends_correct_payload_and_parses_response(self):
        from src.document_preprocessing.preprocess import _process_one

        reply_data = {"file_id": "abc", "text": "hello", "relevant": True}
        mock_post = MagicMock(
            return_value={"replies": [json.dumps(reply_data)]}
        )

        with (
            patch(
                "src.document_preprocessing.preprocess.post_with_retry",
                mock_post,
            ),
            patch(
                "src.document_preprocessing.preprocess.get_id_token",
                return_value="fake-token",
            ),
        ):
            result = _process_one("https://svc.run.app", "gs://bucket/file.pdf")

        assert result == reply_data
        mock_post.assert_called_once()
        call_kwargs = mock_post.call_args
        body = (
            call_kwargs.kwargs.get("body")
            or call_kwargs[1].get("body")
            or call_kwargs[0][2]
            if len(call_kwargs[0]) > 2
            else call_kwargs.kwargs["body"]
        )
        assert body == {"calls": [["gs://bucket/file.pdf"]]}

    def test_passes_auth_header(self):
        from src.document_preprocessing.preprocess import _process_one

        mock_post = MagicMock(
            return_value={
                "replies": ['{"file_id":"x","text":"","relevant":false}']
            }
        )

        with (
            patch(
                "src.document_preprocessing.preprocess.post_with_retry",
                mock_post,
            ),
            patch(
                "src.document_preprocessing.preprocess.get_id_token",
                return_value="my-id-token",
            ),
        ):
            _process_one("https://svc.run.app", "gs://bucket/file.pdf")

        headers = (
            mock_post.call_args[1].get("headers") or mock_post.call_args[0][1]
        )
        assert headers["Authorization"] == "Bearer my-id-token"
